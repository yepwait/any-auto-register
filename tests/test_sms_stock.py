"""接码补库的策略、费用保护和 API 集成；所有真实协议/付费入口均被替换。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool

from api import sms_stock as api
from api import tasks
from core import db
from core.base_platform import Account
from core.proxy_pool import proxy_pool  # 先绑定正式测试引擎，避免懒加载捕获 fixture 的临时引擎
from core.task_runtime import RegisterTaskControl, RegisterTaskStore, StopTaskRequested
from platforms.chatgpt.rt_backfill import BackfillResult
from services import sms_stock_service as service


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    for module in (service, tasks, db):
        monkeypatch.setattr(module, "engine", engine)
    saved = {"sms_enabled": "1", "sms_api_key": "test-only", "mail_provider": "test"}
    monkeypatch.setattr(service.config_store, "get", lambda key, default="": saved.get(key, default))
    monkeypatch.setattr(service.config_store, "get_all", lambda: dict(saved))
    monkeypatch.setattr(service.config_store, "set_many", saved.update)
    monkeypatch.setattr(tasks, "_task_store", RegisterTaskStore())
    monkeypatch.setattr("core.proxy_pool.proxy_pool.get_next", lambda: None)
    monkeypatch.setattr("core.registry.is_platform_enabled", lambda _name: True)
    monkeypatch.setattr("services.sms_service.SmsActivateProvider.get_number", Mock(side_effect=AssertionError("real SMS is forbidden")))
    monkeypatch.setattr("platforms.chatgpt.protocol.AuthFlow.run_register", Mock(side_effect=AssertionError("real registration is forbidden")))
    monkeypatch.setattr(tasks, "_save_task_log", Mock())
    monkeypatch.setattr(tasks, "_auto_upload_integrations", Mock())
    yield saved
    engine.dispose()


def account(*, verified=False, status="registered", platform="chatgpt", extra=None):
    """verified=True 表示“已有可用 RT 的账号”，顺手带上 refresh_token。

    注意：短信标记在本套测试里仍会写入，但新口径下它不再是库存条件。
    """
    payload = {"sms_verification": {"verified": verified}, **(extra or {})}
    if verified and not (payload.get("refresh_token") or payload.get("refreshToken")):
        payload["refresh_token"] = "rt-stock"
    with Session(service.engine) as session:
        row = db.AccountModel(platform=platform, email="stock@example.invalid", password="pw", token="at", status=status)
        row.set_extra(payload)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def client():
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(tasks.router)
    return TestClient(app)


def run(settings=None, *, register=None, should_continue=None, control=None):
    return service.replenish(
        settings or service.SmsStockSettings(min_accounts=1, delay_seconds=0),
        control=control or RegisterTaskControl(), log=lambda _text: None,
        register=register or Mock(return_value=False), progress=lambda *_args: None,
        should_continue=should_continue or (lambda: True),
    )


def test_default_off_and_validated_config_round_trip():
    assert not service.SmsStockSettings.from_store().enabled
    value = service.SmsStockSettings(min_accounts=23, max_attempts_per_run=9)
    service.save_settings(value)
    assert service.SmsStockSettings.from_store() == value


@pytest.mark.parametrize("data", [{"min_accounts": 0}, {"max_attempts_per_run": -1}, {"daily_attempt_limit": 0}, {"interval_minutes": 0}, {"register_flow": "unknown"}, {"delay_seconds": float("inf")}])
def test_rejects_invalid_limits(data):
    with pytest.raises(ValueError):
        service.SmsStockSettings(**data)


def test_inventory_counts_rt_ready_normal_states_only():
    account(verified=True)  # registered + RT
    account(verified=True, status="trial")  # trial + RT
    for status in ["invalid", "expired", "deleted", "banned"]:
        account(verified=True, status=status)  # 非正常状态，不计入
    account(verified=True, platform="cursor")  # 非 chatgpt 平台，不计入
    account(verified=False)  # 无 RT，待补
    account(extra={"refresh_token": "rt"})  # 有 RT，即使没有短信记录也算可用
    value = service.inventory(service.SmsStockSettings(min_accounts=5))
    assert value["verified_accounts"] == 3
    assert value["missing_accounts"] == 2
    assert value["pending_accounts"] == 1


def test_recoverable_invalid_candidate_is_not_discarded_as_dead():
    candidate = account(status="invalid", extra={"chatgpt_local": {"auth": {"state": "unauthorized", "http_status": 401}}})
    dead = account(status="deleted")
    assert service._next_candidate(service.SmsStockSettings(), set()).id == candidate.id
    assert not service._pending(dead)


def test_candidate_cooldown_persists_and_prevents_immediate_repeat():
    candidate = account()
    settings = service.SmsStockSettings()
    assert service.inventory(settings)["ready_candidates"] == 1
    service._mark_attempt(candidate.id)
    assert service.inventory(settings)["ready_candidates"] == 0
    assert service._next_candidate(settings, set()) is None


def test_prioritizes_existing_then_registers_and_stops_at_target(monkeypatch):
    existing = [account(), account()]
    calls = []

    def supplement(**kwargs):
        calls.append("supplement")
        return BackfillResult(success=True, refresh_token="new-rt", sms_verification={"verified": True})

    monkeypatch.setattr("services.chatgpt_rt_backfill.backfill_account_data", supplement)
    monkeypatch.setattr(service, "_probe_after_backfill", lambda *a, **k: "")
    def register(*_args):
        calls.append("register")
        account(verified=True)
        return True
    result = run(service.SmsStockSettings(min_accounts=3, delay_seconds=0), register=register)
    assert calls == ["supplement", "supplement", "register"]
    assert result.success == 3
    assert result.attempts == 3
    assert result.final_inventory == 3
    assert service.daily_attempts_used() == 3
    assert len(existing) == 2


def test_rt_success_without_sms_is_counted(monkeypatch):
    """没有接短信，但拿到有效 RT 且账态正常，就算成功。"""
    account()
    monkeypatch.setattr("services.chatgpt_rt_backfill.backfill_account_data", Mock(return_value=BackfillResult(success=True, refresh_token="rt")))
    monkeypatch.setattr(service, "_probe_after_backfill", lambda *a, **k: "")
    result = run(service.SmsStockSettings(min_accounts=1, max_attempts_per_run=1, delay_seconds=0))
    assert result.success == 1
    assert result.errors == []
    assert result.final_inventory == 1


def test_already_enough_never_reserves_or_registers():
    account(verified=True)
    register = Mock()
    result = run(register=register)
    assert result.attempts == 0
    assert service.daily_attempts_used() == 0
    register.assert_not_called()


def test_per_run_limit_bounds_attempts():
    register = Mock(return_value=False)
    result = run(service.SmsStockSettings(max_attempts_per_run=2, delay_seconds=0), register=register)
    assert result.attempts == 2
    assert register.call_count == 2


def test_consecutive_failures_stop_run_before_cap():
    register = Mock(return_value=False)
    result = run(service.SmsStockSettings(max_attempts_per_run=10, max_consecutive_failures=3, delay_seconds=0), register=register)
    assert result.attempts == 3
    assert "连续失败" in result.reason


def test_daily_limit_survives_multiple_runs():
    settings = service.SmsStockSettings(daily_attempt_limit=2, delay_seconds=0)
    first = run(settings)
    second = run(settings)
    assert first.attempts == 2
    assert second.attempts == 0
    assert service.inventory(settings)["daily_attempts_remaining"] == 0


def test_utc_new_day_resets_budget(isolated):
    isolated.update({"sms_stock_budget_date": "2000-01-01", "sms_stock_budget_used": "100"})
    assert service.daily_attempts_used() == 0
    assert service.reserve_attempt(service.SmsStockSettings(daily_attempt_limit=1))
    assert not service.reserve_attempt(service.SmsStockSettings(daily_attempt_limit=1))


def test_turning_off_prevents_next_operation():
    register = Mock()
    assert run(register=register, should_continue=lambda: False).attempts == 0
    register.assert_not_called()


def test_stop_request_is_propagated_without_registering():
    control = RegisterTaskControl()
    control.request_stop()
    register = Mock()
    with pytest.raises(StopTaskRequested):
        run(control=control, register=register)
    register.assert_not_called()


def test_read_only_status_and_config_do_not_create_tasks(monkeypatch):
    runner = Mock()
    monkeypatch.setattr(api, "_run", runner)
    assert client().get("/sms-stock/status").status_code == 200
    assert client().get("/sms-stock/config").json()["enabled"] is False
    runner.assert_not_called()
    assert tasks._task_store.list_snapshots() == []


def test_enable_and_manual_run_require_cost_confirmation():
    assert client().put("/sms-stock/config", json={"config": {"enabled": True}}).status_code == 400
    assert client().post("/sms-stock/run", json={}).status_code == 400
    assert not service.SmsStockSettings.from_store().enabled


def test_enable_requires_sms_config(isolated):
    isolated["sms_api_key"] = ""
    response = client().put("/sms-stock/config", json={"config": {"enabled": True}, "confirm_cost": True})
    assert response.status_code == 400
    assert not service.SmsStockSettings.from_store().enabled


def test_enable_validated_and_scheduler_woken(monkeypatch):
    wake = Mock()
    monkeypatch.setattr("core.scheduler.scheduler.trigger_sms_stock_now", wake)
    response = client().put("/sms-stock/config", json={"config": {"enabled": True, "min_accounts": 7}, "confirm_cost": True})
    assert response.status_code == 200
    assert service.SmsStockSettings.from_store().min_accounts == 7
    wake.assert_called_once()


def test_invalid_threshold_rejected_without_saving():
    response = client().put("/sms-stock/config", json={"config": {"min_accounts": 0}})
    assert response.status_code == 422
    assert service.SmsStockSettings.from_store().min_accounts == 10


def test_enough_inventory_skips_without_creating_task():
    service.save_settings(service.SmsStockSettings(min_accounts=1))
    account(verified=True)
    response = client().post("/sms-stock/run", json={"confirm_cost": True})
    assert response.status_code == 200
    assert not response.json()["triggered"]
    assert tasks._task_store.list_snapshots() == []


def test_concurrent_gpt_task_blocks_stock():
    tasks._task_store.create("existing", platform="chatgpt", total=1, source="manual")
    assert client().post("/sms-stock/run", json={"confirm_cost": True}).status_code == 409
    assert not api.enqueue(trigger="schedule")["triggered"]  # 默认未开启


def test_duplicate_stock_is_not_enqueued(monkeypatch):
    monkeypatch.setattr(api, "_run", Mock())
    first = api.enqueue(trigger="manual", background_tasks=BackgroundTasks())
    assert first["triggered"]
    with pytest.raises(HTTPException) as exc:
        api.enqueue(trigger="manual", background_tasks=BackgroundTasks())
    assert exc.value.status_code == 409


def test_stock_blocks_conflicting_register_and_backfill():
    tasks._task_store.create("stock", platform="chatgpt", total=1, source=service.SOURCE)
    assert client().post("/tasks/register", json={"platform": "chatgpt"}).status_code == 409
    assert client().post("/tasks/backfill-rt", json={"all_filtered": True}).status_code == 409
    assert client().post("/tasks/bind-2fa", json={"all_filtered": True}).status_code == 409


def test_disable_requests_stop_of_running_stock():
    tasks._task_store.create("stock", platform="chatgpt", total=1, source=service.SOURCE)
    response = client().put("/sms-stock/config", json={"config": {"enabled": False}})
    assert response.status_code == 200
    assert tasks._task_store.control_for("stock").is_stop_requested()


def test_manual_api_supplements_existing_and_persists_summary(monkeypatch):
    service.save_settings(service.SmsStockSettings(min_accounts=1, delay_seconds=0))
    account()
    monkeypatch.setattr("services.chatgpt_rt_backfill.backfill_account_data", Mock(return_value=BackfillResult(success=True, refresh_token="rt", sms_verification={"verified": True})))
    monkeypatch.setattr(service, "_probe_after_backfill", lambda *a, **k: "")
    response = client().post("/sms-stock/run", json={"confirm_cost": True})
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    snapshot = client().get(f"/tasks/{task_id}").json()
    assert snapshot["success"] == 1
    assert snapshot["status"] == "done"
    assert snapshot["meta"]["final_inventory"] == 1
    assert snapshot["meta"]["reason"] == "已达到目标库存"


def test_register_child_strictly_rejects_non_rt_and_shares_control(monkeypatch):
    class Platform:
        def __init__(self, config, mailbox):
            self.mailbox = mailbox
        def bind_task_control(self, control):
            self.control = control
        def register(self, **_kwargs):
            return Account(platform="chatgpt", email="new@example.invalid", password="pw", token="at")
    monkeypatch.setattr("core.registry.get", lambda _name: Platform)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **_kwargs: SimpleNamespace())
    parent = tasks._task_store.create("parent", platform="chatgpt", total=1, source=service.SOURCE)
    ok = api._register_one("parent", service.SmsStockSettings(), parent.control)
    assert not ok
    child_id = tasks._task_store.snapshot("parent")["meta"]["child_task_ids"][0]
    child = tasks._task_store.snapshot(child_id)
    assert child["success"] == 0
    assert "未取得有效 RT" in child["errors"][0]
    assert child["meta"]["account_ids"]
    assert tasks._task_store.control_for(child_id) is parent.control
    tasks._auto_upload_integrations.assert_not_called()


def test_register_child_accepts_actual_rt(monkeypatch):
    class Platform:
        def __init__(self, config, mailbox):
            self.mailbox = mailbox
        def bind_task_control(self, control):
            pass
        def register(self, **_kwargs):
            return Account(platform="chatgpt", email="new@example.invalid", password="pw", token="at", extra={"refresh_token": "rt-new"})
    monkeypatch.setattr("core.registry.get", lambda _name: Platform)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **_kwargs: SimpleNamespace())
    parent = tasks._task_store.create("parent", platform="chatgpt", total=1, source=service.SOURCE)
    assert api._register_one("parent", service.SmsStockSettings(), parent.control)
    assert service.inventory(service.SmsStockSettings())["verified_accounts"] == 1


def test_scheduler_default_off_and_custom_interval():
    from core.scheduler import Scheduler
    scheduler = Scheduler()
    assert scheduler._get_sms_stock_interval_seconds() == 0
    service.save_settings(service.SmsStockSettings(enabled=True, interval_minutes=7))
    assert scheduler._get_sms_stock_interval_seconds() == 420
