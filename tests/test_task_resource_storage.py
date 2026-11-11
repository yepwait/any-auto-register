"""Independent storage/resource review regressions; isolated SQLite, no network."""

import asyncio
from collections import deque
from contextlib import contextmanager
import json
import threading
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from api import tasks
import core.db as db
import core.resource_limits as limits
from core.db import TaskRunLogModel, TaskRunModel
from core.task_runtime import AttemptResult, RegisterTaskStore


@pytest.fixture
def storage(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'tasks.sqlite').as_posix()}")
    SQLModel.metadata.create_all(engine)
    store = RegisterTaskStore(max_log_lines=2, max_log_bytes=256)
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "_task_store", store)
    monkeypatch.setattr(tasks, "_pending_log_lines", deque())
    monkeypatch.setattr(tasks, "_dirty_task_ids", set())
    monkeypatch.setattr(tasks, "_log_pending_bytes", 0)
    monkeypatch.setattr(tasks, "_task_admission_closed", False)
    # Exercise real production buffering without a timing-dependent writer tick.
    monkeypatch.setattr(tasks, "_log_writer_running", True)
    monkeypatch.setattr(tasks, "_log_writer_thread", None)
    monkeypatch.setattr(tasks, "_log_writer_stop", threading.Event())
    monkeypatch.setattr(tasks, "print", mock.Mock(), raising=False)
    yield engine, store
    engine.dispose()


def create_task(store, task_id="review", *, source="manual", status=None, meta=None):
    store.create(task_id, platform="fake", source=source, total=6, meta=meta)
    tasks._persist_task_snapshot(task_id)
    if status:
        store.finish(task_id, status=status, success=0, skipped=0, errors=[])
        tasks._persist_task_snapshot(task_id)
    return task_id


@contextmanager
def statements(engine):
    captured = []
    def before_cursor_execute(_conn, _cursor, statement, _params, _context, _many):
        captured.append(statement)
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield captured
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def assert_read_only(sql):
    writes = [statement for statement in sql if statement.lstrip().split()[0].lower()
              in {"insert", "update", "delete", "alter", "create", "replace"}]
    assert writes == []


def paged_lines(task_id, *, limit=2):
    lines, cursor = [], 0
    for _ in range(100):
        page = tasks._task_log_page(task_id, cursor, limit)
        assert page["log_offset"] == cursor, "An absolute cursor silently skipped log history"
        lines.extend(page["logs"])
        if page["next_since"] >= page["log_count"]:
            return lines
        assert page["next_since"] > cursor, "Pagination made no forward progress"
        cursor = page["next_since"]
    pytest.fail("Pagination did not terminate")


def test_get_list_detail_logs_and_sse_never_write(storage):
    engine, store = storage
    task_id = create_task(store, status="done")
    tasks._log(task_id, "persisted")
    tasks.flush_task_persistence()
    store.remove(task_id)  # Exercise database reads, not only the live fast path.
    app = FastAPI()
    app.include_router(tasks.router)
    with TestClient(app) as client:
        with statements(engine) as sql:
            for path in ("/tasks", "/tasks/page", f"/tasks/{task_id}",
                         f"/tasks/{task_id}/logs", f"/tasks/{task_id}/logs/stream"):
                response = client.get(path)
                assert response.status_code == 200, response.text
            assert '"done": true' in response.text
        assert_read_only(sql)


def test_new_lines_append_and_snapshot_updates_never_rewrite_logs_json(storage):
    engine, store = storage
    task_id = create_task(store)
    with statements(engine) as sql:
        for index in range(10):
            tasks._log(task_id, f"line-{index}")
        tasks.flush_task_persistence()
        store.finish(task_id, status="done", success=6, skipped=0, errors=[])
        tasks._persist_task_snapshot(task_id)
    with Session(engine) as session:
        row = session.get(TaskRunModel, task_id)
        lines = session.exec(select(TaskRunLogModel).where(TaskRunLogModel.task_id == task_id)
                             .order_by(TaskRunLogModel.sequence)).all()
    assert row.logs_version == 1
    assert row.logs_json == "[]"
    assert row.log_count == 10
    assert [line.sequence for line in lines] == list(range(10))
    assert [line.line.rsplit(" ", 1)[-1] for line in lines] == [f"line-{i}" for i in range(10)]
    updates = [statement.lower() for statement in sql if statement.lstrip().lower().startswith("update")]
    assert all("logs_json" not in statement.split("where", 1)[0] for statement in updates)


def test_evicted_durable_tail_has_complete_absolute_pagination(storage):
    _engine, store = storage
    task_id = create_task(store)
    for index in range(6):
        tasks._log(task_id, f"line-{index}")
    tasks.flush_task_persistence()
    snapshot = tasks._get_task_snapshot(task_id)
    assert snapshot["log_offset"] == 4
    assert snapshot["log_count"] == snapshot["next_since"] == 6
    assert len(snapshot["logs"]) == 2
    expected = [f"line-{index}" for index in range(6)]
    assert [line.rsplit(" ", 1)[-1] for line in paged_lines(task_id)] == expected
    store.remove(task_id)
    assert [line.rsplit(" ", 1)[-1] for line in paged_lines(task_id)] == expected


def test_pending_eviction_does_not_skip_lines_before_writer_tick(storage):
    engine, store = storage
    task_id = create_task(store)
    for index in range(6):
        tasks._log(task_id, f"line-{index}")
    assert len(tasks._pending_log_lines) == 6
    with statements(engine) as sql:
        lines = paged_lines(task_id)
    assert_read_only(sql)
    assert [line.rsplit(" ", 1)[-1] for line in lines] == [f"line-{index}" for index in range(6)]


def test_sse_before_writer_tick_never_resets_past_pending_history(storage):
    _engine, store = storage
    task_id = create_task(store)
    for index in range(6):
        tasks._log(task_id, f"line-{index}")
    store.finish(task_id, status="done", success=1, skipped=0, errors=[])
    async def collect():
        response = await tasks.stream_logs(task_id, since=0)
        events = []
        async for chunk in response.body_iterator:
            events.append(json.loads(chunk.split("data: ", 1)[1].strip()))
        return events
    events = asyncio.run(asyncio.wait_for(collect(), timeout=2))
    lines = [item for item in events if "line" in item]
    assert [item["sequence"] for item in lines] == list(range(6))
    assert [item["line"].rsplit(" ", 1)[-1] for item in lines] == [f"line-{i}" for i in range(6)]
    assert events[-1]["done"] is True


def test_durable_append_cursor_wins_after_snapshot_commit_failure(storage, monkeypatch):
    engine, store = storage
    task_id = create_task(store, status="done")
    for index in range(2):
        tasks._log(task_id, f"line-{index}")
    tasks.flush_task_persistence()
    for index in range(2, 6):
        tasks._log(task_id, f"line-{index}")
    with mock.patch.object(tasks, "_upsert_task_run", side_effect=RuntimeError("snapshot failed")):
        with pytest.raises(RuntimeError, match="snapshot failed"):
            tasks.flush_task_persistence()
    # Log commit succeeded, then a crash/restart loses the live snapshot.
    store.remove(task_id)
    with Session(engine) as session:
        row = session.get(TaskRunModel, task_id)
        # Append and its count are atomic even when the later snapshot fails.
        assert row.log_count == 6
        # Old/interrupted deployments can still contain a stale summary count.
        row.log_count = 2
        session.add(row)
        session.commit()
    assert len(paged_lines(task_id, limit=2)) == 6
    async def collect():
        response = await tasks.stream_logs(task_id, since=0)
        return [json.loads(chunk.split("data: ", 1)[1].strip()) async for chunk in response.body_iterator]
    events = asyncio.run(asyncio.wait_for(collect(), timeout=2))
    assert len([item for item in events if "line" in item]) == 6


def test_summary_sql_applies_source_status_paging_without_large_bodies(storage):
    engine, store = storage
    create_task(store, "manual", source="manual", status="done")
    create_task(store, "inspection-active", source="inspection")
    create_task(store, "inspection-done", source="inspection", status="done")
    # Stored history may be huge; summary/count must never fetch these columns.
    with Session(engine) as session:
        row = session.get(TaskRunModel, "inspection-done")
        row.logs_json = json.dumps(["large legacy log"] * 1000)
        row.errors_json = json.dumps(["large legacy error"] * 1000)
        session.add(row)
        session.commit()
    with statements(engine) as sql:
        result = tasks._list_persisted_tasks(source="inspection", status="finished", limit=1, offset=0)
        total = tasks._count_task_summaries(source="inspection", status="finished")
    assert [item["id"] for item in result] == ["inspection-done"]
    assert total == 1
    for statement in sql:
        assert "logs_json" not in statement.lower()
        assert "errors_json" not in statement.lower()
    assert_read_only(sql)


def test_legacy_json_remains_readable_in_detail_and_pagination(storage):
    engine, store = storage
    old_lines = [f"old-{index}" for index in range(6)]
    with Session(engine) as session:
        session.add(TaskRunModel(id="legacy", platform="fake", status="done", logs_version=0,
                                 logs_json=json.dumps(old_lines), log_count=6, errors_json='["old-error"]'))
        session.commit()
    with statements(engine) as sql:
        snapshot = tasks._get_task_snapshot("legacy")
        lines = paged_lines("legacy")
    assert snapshot["logs"] == old_lines
    assert snapshot["errors"] == ["old-error"]
    assert lines == old_lines
    assert_read_only(sql)


def test_new_orphan_restart_tip_uses_append_table_and_updates_cursor(storage):
    engine, store = storage
    task_id = create_task(store)
    tasks._log(task_id, "before restart")
    tasks.flush_task_persistence()
    store.remove(task_id)
    tasks._finalize_orphan_tasks()
    with Session(engine) as session:
        row = session.get(TaskRunModel, task_id)
    snapshot = tasks._get_task_snapshot(task_id)
    assert row.status == "stopped"
    assert row.logs_json == "[]", "New tasks must not regress to whole-history JSON writes"
    assert row.log_count == 2
    assert snapshot["next_since"] == 2
    assert any("服务重启中断" in line for line in snapshot["logs"])


def test_additive_legacy_migration_is_repeatable_and_keeps_json(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.sqlite').as_posix()}")
    monkeypatch.setattr(db, "engine", engine)
    old_logs, old_errors = '["one", "two"]', '["error"]'
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TABLE task_runs (
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'done', total INTEGER NOT NULL DEFAULT 0,
                progress TEXT NOT NULL DEFAULT '0/0', success INTEGER NOT NULL DEFAULT 0,
                registered INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', meta_json TEXT NOT NULL DEFAULT '{}',
                logs_json TEXT NOT NULL DEFAULT '[]', errors_json TEXT NOT NULL DEFAULT '[]',
                cashier_urls_json TEXT NOT NULL DEFAULT '[]', control_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME, updated_at DATETIME)""")
            connection.exec_driver_sql(
                "INSERT INTO task_runs (id, platform, logs_json, errors_json) VALUES (?, ?, ?, ?)",
                ("legacy", "fake", old_logs, old_errors),
            )
        db.init_db()
        db.init_db()
        with Session(engine) as session:
            row = session.get(TaskRunModel, "legacy")
        assert (row.log_count, row.error_count, row.logs_version) == (2, 1, 0)
        assert (row.logs_json, row.errors_json) == (old_logs, old_errors)
        with engine.connect() as connection:
            columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info('task_runs')")]
            assert columns.count("log_count") == columns.count("logs_version") == 1
            assert connection.exec_driver_sql("SELECT count(*) FROM task_run_logs").scalar_one() == 0
    finally:
        engine.dispose()


def test_failed_append_commit_preserves_buffer_and_retry_is_exactly_once(storage):
    engine, store = storage
    task_id = create_task(store)
    tasks._log(task_id, "critical")
    original = list(tasks._pending_log_lines)
    with mock.patch.object(tasks.Session, "commit", side_effect=RuntimeError("temporary failure")):
        with pytest.raises(RuntimeError, match="temporary failure"):
            tasks._flush_task_logs()
    assert list(tasks._pending_log_lines) == original
    tasks.flush_task_persistence()
    tasks.flush_task_persistence()
    with Session(engine) as session:
        persisted = session.exec(select(TaskRunLogModel).where(TaskRunLogModel.task_id == task_id)).all()
    assert len(persisted) == 1
    assert persisted[0].sequence == 0
    assert tasks._log_pending_bytes == 0


def test_batch_finish_flushes_every_accepted_log_before_return(storage, monkeypatch):
    engine, store = storage
    task_id = create_task(store)
    monkeypatch.setattr(tasks, "_load_account_fields", lambda account_id: {"email": f"{account_id}@example.com"})
    monkeypatch.setattr(limits, "_account_limiter", limits.AccountLimiter(1))
    monkeypatch.setattr("core.proxy_pool.proxy_pool.get_next", lambda: None)
    def handle(**kwargs):
        tasks._log(task_id, f"critical-{kwargs['account_id']}")
        return AttemptResult.success()
    tasks._run_account_batch_task(task_id, [1, 2, 3], label="mock", concurrency=20, handle_account=handle)
    assert tasks._pending_log_lines == deque()
    assert task_id not in tasks._dirty_task_ids
    with Session(engine) as session:
        row = session.get(TaskRunModel, task_id)
        lines = session.exec(select(TaskRunLogModel).where(TaskRunLogModel.task_id == task_id)
                             .order_by(TaskRunLogModel.sequence)).all()
    assert row.status == "done"
    assert row.success == 3
    assert len(lines) == row.log_count
    assert [line.sequence for line in lines] == list(range(row.log_count))
    assert len([line for line in lines if "critical-" in line.line]) == 3
    assert "完成" in lines[-1].line


def test_shutdown_closes_admission_stops_shared_controls_and_late_logs_are_durable(storage, monkeypatch):
    import services.background_jobs as background
    engine, store = storage
    parent_id = create_task(store, "parent")
    child_id = create_task(store, "child", meta={"parent_task_id": parent_id})
    control = store.control_for(parent_id)
    store.bind_control(child_id, control)
    assert tasks.stop_task_producers(timeout=0) is False
    assert tasks._task_admission_closed is True
    assert control.is_stop_requested()
    assert store.control_for(child_id).is_stop_requested()
    with pytest.raises(Exception) as caught:
        tasks.ensure_task_capacity()
    assert caught.value.status_code == 503
    queue_submit = mock.Mock(side_effect=AssertionError("closed queue must not be used"))
    monkeypatch.setattr(background, "submit_task_log", queue_submit)
    tasks._save_task_log("fake", "late@example.com", "success", detail={"source": "late"})
    queue_submit.assert_not_called()
    with Session(engine) as session:
        row = session.exec(select(db.TaskLog).where(db.TaskLog.email == "late@example.com")).one()
    assert row.status == "success"
    assert json.loads(row.detail_json) == {"source": "late"}
    monkeypatch.setattr(background, "submit_upload", queue_submit)
    tasks._auto_upload_integrations(parent_id, object())
    queue_submit.assert_not_called()
    assert any("未发起新的外部同步" in line for line in store.snapshot(parent_id)["logs"])
    for task_id in (parent_id, child_id):
        store.finish(task_id, status="stopped", success=0, skipped=0, errors=[])
        tasks._persist_task_snapshot(task_id)
    assert tasks.stop_task_producers(timeout=0) is True


def test_repeated_writer_start_stop_has_no_thread_leak_or_final_log_loss(storage):
    engine, store = storage
    task_id = create_task(store)
    tasks.stop_task_persistence()
    workers = []
    for index in range(3):
        tasks.start_task_persistence()
        worker = tasks._log_writer_thread
        assert worker.is_alive()
        workers.append(worker)
        tasks.start_task_persistence()
        assert tasks._log_writer_thread is worker
        tasks._log(task_id, f"tail-{index}")
        tasks.stop_task_persistence()
        assert not worker.is_alive()
        assert tasks._pending_log_lines == deque()
    assert len(set(workers)) == 3
    with Session(engine) as session:
        lines = session.exec(select(TaskRunLogModel).where(TaskRunLogModel.task_id == task_id)).all()
    assert len(lines) == 3


def test_parent_child_share_control_without_parent_holding_slot(storage, monkeypatch):
    from api import sms_stock
    from core.base_platform import Account
    _engine, store = storage
    parent_id = create_task(store, "parent", source="sms_stock")
    shared_control = store.control_for(parent_id)
    monkeypatch.setattr(limits, "_account_limiter", limits.AccountLimiter(1))
    monkeypatch.setattr("core.proxy_pool.proxy_pool.get_next", lambda: None)
    monkeypatch.setattr("core.config_store.config_store.get_all", lambda: {})
    monkeypatch.setattr("core.config_store.config_store.get", lambda _key, default="": default)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **_kwargs: None)
    monkeypatch.setattr("core.db.save_account", lambda account: account)
    monkeypatch.setattr("services.chatgpt_account_state.account_rt_ready", lambda _account: True)
    monkeypatch.setattr(tasks, "_save_task_log", mock.Mock())
    monkeypatch.setattr(tasks, "_auto_upload_integrations", mock.Mock())
    class FakePlatform:
        def __init__(self, config, mailbox):
            self.mailbox = mailbox
        def bind_task_control(self, control):
            assert control is shared_control
        def register(self, **kwargs):
            return Account(platform="chatgpt", email="mock@example.com", password="pw")
    monkeypatch.setattr("core.registry.get", lambda _name: FakePlatform)
    settings = sms_stock.service.SmsStockSettings()
    values, errors = [], []
    def run():
        try:
            values.append(sms_stock._register_one(parent_id, settings, shared_control))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive(), "Parent held the only account slot while waiting for its child"
    assert errors == []
    assert values == [True]
    child_ids = store.snapshot(parent_id)["meta"]["child_task_ids"]
    assert len(child_ids) == 1
    assert store.control_for(child_ids[0]) is shared_control
    assert store.snapshot(child_ids[0])["status"] == "done"
    assert shared_control.snapshot()["active_attempts"] == 0


def test_two_large_tasks_share_cap_stop_and_leave_no_account_workers(storage, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor as RealExecutor
    _engine, store = storage
    monkeypatch.setattr(limits, "_account_limiter", limits.AccountLimiter(2))
    monkeypatch.setattr("core.proxy_pool.proxy_pool.get_next", lambda: None)
    monkeypatch.setattr(tasks, "_load_account_fields", lambda account_id: {"email": f"{account_id}@example.com"})
    ids = [create_task(store, task_id) for task_id in ("first", "second")]
    lock, entered, release = threading.Lock(), threading.Event(), threading.Event()
    active, peak, calls = 0, 0, 0
    executors, errors = [], []
    class RecordingExecutor(RealExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            executors.append(self)
    def handle(**_kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
        try:
            assert release.wait(3)
            return AttemptResult.success()
        finally:
            with lock:
                active -= 1
    def run(task_id):
        try:
            tasks._run_account_batch_task(task_id, list(range(10000)), label="mock", concurrency=20,
                                         handle_account=handle)
        except BaseException as exc:
            errors.append(exc)
    monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", RecordingExecutor)
    threads = [threading.Thread(target=run, args=(task_id,), daemon=True) for task_id in ids]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(2)
        for task_id in ids:
            store.request_stop(task_id)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert peak == 2
    assert calls <= 4
    assert len(executors) == 2
    assert all(pool._max_workers == 2 for pool in executors)
    assert all(not worker.is_alive() for pool in executors for worker in pool._threads)
    assert all(store.snapshot(task_id)["status"] == "stopped" for task_id in ids)
    assert all(store.control_for(task_id).snapshot()["active_attempts"] == 0 for task_id in ids)
