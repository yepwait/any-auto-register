"""Payment admission/resource regressions: isolated SQLite and mocked channels."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import threading
import time
from unittest import mock
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, SQLModel, create_engine

from api import payments, tasks
import core.resource_limits as limits
from core.db import AccountModel, TaskRunModel, get_session
from core.task_runtime import RegisterTaskStore
from services.payment_channels.contracts import PaymentAccount, PaymentResult


@pytest.fixture
def payment_state(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'payments.sqlite').as_posix()}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for account_id, platform in [(1, "chatgpt"), (2, "chatgpt"), (3, "cursor")]:
            account = AccountModel(
                id=account_id, platform=platform, email=f"{account_id}@example.com", password="pw", token="access"
            )
            account.set_extra({"session_token": "session"})
            session.add(account)
        session.commit()
    store = RegisterTaskStore()
    monkeypatch.setattr(payments, "engine", engine)
    monkeypatch.setattr(tasks, "engine", engine)
    monkeypatch.setattr(tasks, "_task_store", store)
    monkeypatch.setattr(tasks, "_pending_log_lines", deque())
    monkeypatch.setattr(tasks, "_dirty_task_ids", set())
    monkeypatch.setattr(tasks, "_log_pending_bytes", 0)
    monkeypatch.setattr(tasks, "_task_admission_closed", False)
    monkeypatch.setattr(tasks, "_log_writer_running", False)
    monkeypatch.setattr(tasks, "print", mock.Mock(), raising=False)
    monkeypatch.setenv("APP_MAX_ACTIVE_TASKS", "4")
    monkeypatch.setattr(limits, "_account_limiter", limits.AccountLimiter(1))
    monkeypatch.setattr(payments, "_require_channel", mock.Mock())
    monkeypatch.setattr(payments.config_store, "get", lambda _key, default="": default)
    monkeypatch.setattr("core.proxy_pool.proxy_pool.get_next", lambda: None)
    # No test can accidentally enter a real payment transport.
    for name in ("create_link_for_context", "pay_for_context"):
        monkeypatch.setattr(payments, name, mock.Mock(side_effect=AssertionError("unmocked channel")))
    yield SimpleNamespace(engine=engine, store=store)
    engine.dispose()


@pytest.fixture
def client(payment_state):
    app = FastAPI()
    app.include_router(payments.router)
    app.include_router(tasks.router)

    def session_dependency():
        with Session(payment_state.engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    with TestClient(app) as test_client:
        yield test_client


def create_job(state, background=None, **kwargs):
    request = payments.PaymentTaskRequest(account_ids=[1], **kwargs)
    with Session(state.engine) as session:
        return payments.create_payment_task(request, background or BackgroundTasks(), session)


@pytest.mark.parametrize(("closed", "expected_status"), [(False, 429), (True, 503)])
def test_jobs_reject_full_or_closed_queue_without_creating_records(
    payment_state, client, monkeypatch, closed, expected_status
):
    monkeypatch.setenv("APP_MAX_ACTIVE_TASKS", "1")
    payment_state.store.create("existing", platform="chatgpt", total=1, source="manual")
    before = payment_state.store.snapshot("existing")
    monkeypatch.setattr(tasks, "_task_admission_closed", closed)
    runner = mock.Mock()
    monkeypatch.setattr(payments, "_run_payment_task", runner)

    response = client.post("/payments/jobs", json={"account_ids": [1], "operation": "pay"})

    assert response.status_code == expected_status
    assert payment_state.store.list_snapshots(include_logs=True) == [before]
    runner.assert_not_called()


def test_capacity_check_and_creation_share_the_enqueue_lock(payment_state, monkeypatch):
    monkeypatch.setenv("APP_MAX_ACTIVE_TASKS", "1")
    barrier = threading.Barrier(2)
    owner = threading.local()
    lock = threading.RLock()

    class EnqueueLock:
        def __enter__(self):
            lock.acquire()
            owner.held = True

        def __exit__(self, *_exc):
            owner.held = False
            lock.release()

    monkeypatch.setattr(tasks, "chatgpt_enqueue_lock", EnqueueLock())
    ensure_capacity = tasks.ensure_task_capacity
    create = payment_state.store.create

    def checked_capacity():
        assert owner.held
        ensure_capacity()

    def checked_create(*args, **kwargs):
        assert owner.held
        return create(*args, **kwargs)

    monkeypatch.setattr(tasks, "ensure_task_capacity", checked_capacity)
    monkeypatch.setattr(payment_state.store, "create", checked_create)

    def submit():
        barrier.wait(timeout=3)
        try:
            return create_job(payment_state)
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(429) == 1
    assert len(payment_state.store.list_snapshots()) == 1


@pytest.mark.parametrize("operation", ["link", "pay"])
def test_job_runs_each_account_once_in_first_seen_order(payment_state, client, monkeypatch, operation):
    visited = []

    def channel(context, name, *, options):
        assert isinstance(context, PaymentAccount)
        assert payment_state.engine.pool.checkedout() == 0
        assert limits.get_account_limiter()._local.depth == 1
        visited.append(context.account_id)
        return PaymentResult.success(name, operation)

    backend = "create_link_for_context" if operation == "link" else "pay_for_context"
    monkeypatch.setattr(payments, backend, channel)
    response = client.post(
        "/payments/jobs", json={"account_ids": [2, 1, 2, 1], "operation": operation}
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    snapshot = client.get(f"/tasks/{response.json()['task_id']}").json()
    assert snapshot["status"] == "done"
    assert snapshot["success"] == snapshot["total"] == 2
    assert snapshot["meta"]["account_ids"] == [2, 1]
    assert visited == ["2", "1"]
    assert len(snapshot["meta"]["results"]) == 2


@pytest.mark.parametrize(("count", "status"), [(0, 422), (1, 200), (1000, 200), (1001, 422)])
def test_job_preserves_raw_request_size_limits(payment_state, client, monkeypatch, count, status):
    runner = mock.Mock()
    monkeypatch.setattr(payments, "_run_payment_task", runner)
    response = client.post("/payments/jobs", json={"account_ids": [1] * count})
    assert response.status_code == status
    if status == 200:
        assert response.json()["total"] == 1
        runner.assert_called_once()
    else:
        runner.assert_not_called()
        assert payment_state.store.list_snapshots() == []


def test_same_millisecond_jobs_have_distinct_uuid_ids(payment_state):
    with mock.patch.object(time, "time", return_value=1234567890.123):
        first = create_job(payment_state)
        second = create_job(payment_state)
    assert first["task_id"] != second["task_id"]
    for result in (first, second):
        task_id = result["task_id"]
        assert task_id.startswith("payment_link_")
        assert UUID(task_id.removeprefix("payment_link_")).version == 4
        assert payment_state.store.snapshot(task_id)["status"] == "pending"
    assert len(payment_state.store.list_snapshots()) == 2


@pytest.mark.parametrize("failure_stage", ["persist", "log", "schedule"])
def test_enqueue_failure_retains_failed_record_and_releases_capacity(
    payment_state, monkeypatch, failure_stage
):
    monkeypatch.setenv("APP_MAX_ACTIVE_TASKS", "1")
    background = BackgroundTasks()
    with monkeypatch.context() as patch:
        if failure_stage == "persist":
            original = tasks._persist_task_snapshot
            attempts = 0

            def fail_first(task_id):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("write unavailable")
                return original(task_id)

            patch.setattr(tasks, "_persist_task_snapshot", fail_first)
        elif failure_stage == "log":
            patch.setattr(tasks, "_log", mock.Mock(side_effect=RuntimeError("log unavailable")))
        else:
            patch.setattr(background, "add_task", mock.Mock(side_effect=RuntimeError("queue unavailable")))
        with pytest.raises(HTTPException) as caught:
            create_job(payment_state, background)

    assert caught.value.status_code == 500
    task_id = caught.value.detail["task_id"]
    snapshot = payment_state.store.snapshot(task_id)
    assert snapshot["status"] == "failed"
    assert snapshot["total"] == 1
    assert snapshot["registered"] == snapshot["success"] == 0
    assert snapshot["meta"]["account_ids"] == [1]
    assert "调度失败" in snapshot["error"]
    assert background.tasks == []
    with Session(payment_state.engine) as session:
        assert session.get(TaskRunModel, task_id).status == "failed"
    tasks.ensure_task_capacity()
    assert create_job(payment_state)["task_id"] != task_id
    assert payment_state.store.exists(task_id)


def test_persistent_storage_failure_keeps_record_for_writer_retry(payment_state, monkeypatch):
    payment_state.store.max_finished_tasks = 0
    payment_state.store.cleanup_threshold = 0
    with monkeypatch.context() as patch:
        patch.setattr(tasks, "_persist_task_snapshot", mock.Mock(side_effect=RuntimeError("database down")))
        patch.setattr(tasks, "_log", mock.Mock(side_effect=RuntimeError("database down")))
        with pytest.raises(HTTPException) as caught:
            create_job(payment_state)
    task_id = caught.value.detail["task_id"]
    assert payment_state.store.snapshot(task_id)["status"] == "failed"
    assert task_id in tasks._dirty_task_ids
    tasks.ensure_task_capacity()
    tasks.flush_task_persistence()
    assert task_id not in tasks._dirty_task_ids
    with Session(payment_state.engine) as session:
        assert session.get(TaskRunModel, task_id).status == "failed"


@pytest.mark.parametrize("has_progress", [False, True])
def test_scheduler_failure_keeps_accepted_state_and_progress(payment_state, monkeypatch, has_progress):
    task_id = create_job(payment_state)["task_id"]

    def fail_scheduler(*_args, **_kwargs):
        if has_progress:
            payment_state.store.mark_running(task_id)
            payment_state.store.update_counters(task_id, success=1, registered=1)
            payment_state.store.append_meta_list(task_id, "results", {"account_id": 1, "ok": True})
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(tasks, "_run_account_batch_task", fail_scheduler)
    payments._run_payment_task(task_id, [1], "link", "direct", {}, 1, 0)
    snapshot = payment_state.store.snapshot(task_id)
    assert snapshot["status"] == "failed"
    assert snapshot["success"] == snapshot["registered"] == int(has_progress)
    assert len(snapshot["meta"]["results"]) == int(has_progress)
    assert "scheduler unavailable" in snapshot["error"]
    tasks.ensure_task_capacity()
    with Session(payment_state.engine) as session:
        assert session.get(TaskRunModel, task_id).status == "failed"


def test_final_persistence_failure_preserves_completed_payment_results(payment_state, client, monkeypatch):
    original = tasks._persist_task_snapshot
    failed_once = False

    def fail_final_write(task_id):
        nonlocal failed_once
        if payment_state.store.snapshot(task_id)["status"] == "done" and not failed_once:
            failed_once = True
            raise RuntimeError("final write unavailable")
        return original(task_id)

    channel = mock.Mock(return_value=PaymentResult.success("direct", "pay"))
    monkeypatch.setattr(tasks, "_persist_task_snapshot", fail_final_write)
    monkeypatch.setattr(payments, "pay_for_context", channel)
    response = client.post("/payments/jobs", json={"account_ids": [1], "operation": "pay"})
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    snapshot = payment_state.store.snapshot(task_id)
    assert failed_once
    assert snapshot["status"] == "done"
    assert snapshot["success"] == snapshot["registered"] == 1
    assert len(snapshot["meta"]["results"]) == 1
    channel.assert_called_once()
    with Session(payment_state.engine) as session:
        assert session.get(TaskRunModel, task_id).status == "done"


@pytest.mark.parametrize("operation", ["link", "pay"])
@pytest.mark.parametrize("entrypoint", ["http", "direct", "explicit_session"])
def test_single_payment_closes_session_before_slot_and_network(
    payment_state, client, monkeypatch, operation, entrypoint
):
    acquired = False

    @contextmanager
    def slot():
        nonlocal acquired
        assert payment_state.engine.pool.checkedout() == 0
        with limits.account_slot():
            acquired = True
            try:
                yield
            finally:
                acquired = False

    def channel(context, name, *, options):
        assert acquired
        assert payment_state.engine.pool.checkedout() == 0
        assert isinstance(context, PaymentAccount)
        assert context.account_id == "1"
        assert context.email == "1@example.com"
        assert context.access_token == "access"
        assert context.session_token == "session"
        assert options == {"country": "PH"}
        return PaymentResult.success(name, operation, link="mock-link")

    def unexpected_dependency():
        raise AssertionError("single-payment HTTP routes must own their short session")

    client.app.dependency_overrides[get_session] = unexpected_dependency
    monkeypatch.setattr(payments, "account_slot", slot)
    backend = "create_link_for_context" if operation == "link" else "pay_for_context"
    monkeypatch.setattr(payments, backend, channel)
    request = payments.PaymentRequest(options={"country": "PH"})
    endpoint = payments.create_payment_link if operation == "link" else payments.execute_payment
    if entrypoint == "http":
        response = client.post(f"/payments/1/{operation}", json=request.model_dump())
        assert response.status_code == 200
        result = response.json()
    elif entrypoint == "explicit_session":
        with Session(payment_state.engine) as session:
            result = endpoint(1, request, session)
            assert not session.in_transaction()
    else:
        result = endpoint(1, request)
    assert result == PaymentResult.success("direct", operation, link="mock-link").as_dict()
    assert not acquired


@pytest.mark.parametrize("operation", ["link", "pay"])
@pytest.mark.parametrize("account_id", [3, 99999])
def test_single_payment_rejects_non_chatgpt_or_missing_account(
    payment_state, client, monkeypatch, operation, account_id
):
    slot = mock.Mock(side_effect=AssertionError("invalid accounts must not acquire slots"))
    monkeypatch.setattr(payments, "account_slot", slot)
    response = client.post(f"/payments/{account_id}/{operation}", json={})
    assert response.status_code == 404
    slot.assert_not_called()
    payments.create_link_for_context.assert_not_called()
    payments.pay_for_context.assert_not_called()
    assert payment_state.engine.pool.checkedout() == 0


@pytest.mark.parametrize("operation", ["link", "pay"])
def test_single_payment_waits_for_shared_worker_without_holding_connection(
    payment_state, monkeypatch, operation
):
    limiter = limits.get_account_limiter()
    semaphore = limiter._semaphore
    waiting = threading.Event()
    main_thread = threading.get_ident()

    class ObservedSemaphore:
        def acquire(self, **kwargs):
            if threading.get_ident() != main_thread:
                waiting.set()
            return semaphore.acquire(**kwargs)

        def release(self):
            return semaphore.release()

    monkeypatch.setattr(limiter, "_semaphore", ObservedSemaphore())
    channel = mock.Mock(return_value=PaymentResult.success("direct", operation))
    backend = "create_link_for_context" if operation == "link" else "pay_for_context"
    monkeypatch.setattr(payments, backend, channel)
    endpoint = payments.create_payment_link if operation == "link" else payments.execute_payment
    with ThreadPoolExecutor(max_workers=1) as pool:
        with limits.account_slot():
            future = pool.submit(endpoint, 1, payments.PaymentRequest())
            assert waiting.wait(timeout=3)
            channel.assert_not_called()
            assert payment_state.engine.pool.checkedout() == 0
        assert future.result(timeout=3)["ok"]
    channel.assert_called_once()


@pytest.mark.parametrize("operation", ["link", "pay"])
def test_single_payment_exception_releases_global_slot(payment_state, monkeypatch, operation):
    channel = mock.Mock(side_effect=RuntimeError("mock channel failed"))
    backend = "create_link_for_context" if operation == "link" else "pay_for_context"
    monkeypatch.setattr(payments, backend, channel)
    endpoint = payments.create_payment_link if operation == "link" else payments.execute_payment
    with pytest.raises(RuntimeError, match="mock channel failed"):
        endpoint(1, payments.PaymentRequest())
    assert payment_state.engine.pool.checkedout() == 0
    limiter = limits.get_account_limiter()
    assert limiter._semaphore.acquire(blocking=False), "failed payment leaked its worker slot"
    limiter._semaphore.release()
