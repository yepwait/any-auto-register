"""Web pool contract and task isolation. All HTTP/registration/SMS are mocked."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from api import chatgpt_web_pool as api
from api import tasks
from core import db
from core.base_platform import Account
from core.proxy_pool import proxy_pool
from core.task_runtime import RegisterTaskControl, RegisterTaskStore, StopTaskRequested
from services import chatgpt_web_pool as service
from services import inspection_service, sms_stock_service
from services.chatgpt_account_purpose import WEB_POOL_PURPOSE


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    for module in (service, tasks, db, inspection_service, sms_stock_service):
        monkeypatch.setattr(module, "engine", engine)
    saved = {"sms_enabled": "1", "chatgpt_register_flow": "phone_with_email", "chatgpt_registration_mode": "refresh_token", "chatgpt_bind_2fa": "1", "chatgpt_require_rt_ready": "1", "mail_provider": "test"}
    monkeypatch.setattr(service.config_store, "get", lambda key, default="": saved.get(key, default))
    monkeypatch.setattr(service.config_store, "get_all", lambda: dict(saved))
    monkeypatch.setattr(service.config_store, "set_many", saved.update)
    monkeypatch.setattr(tasks, "_task_store", RegisterTaskStore())
    monkeypatch.setattr(tasks, "_save_task_log", Mock())
    monkeypatch.setattr(tasks, "_auto_upload_integrations", Mock())
    monkeypatch.setattr(proxy_pool, "get_next", lambda: None)
    monkeypatch.setattr("core.registry.is_platform_enabled", lambda _name: True)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr("services.sms_service.SmsActivateProvider.get_number", Mock(side_effect=AssertionError("SMS forbidden")))
    monkeypatch.setattr("platforms.chatgpt.protocol.AuthFlow.run_register", Mock(side_effect=AssertionError("real registration forbidden")))
    yield saved
    engine.dispose()


def settings(**changes):
    return service.WebPoolSettings(base_url="https://pool.example.invalid/prefix", admin_token="test-admin-secret", delay_seconds=0, **changes)


def remote(token="at", quota=5, status="正常", **extra):
    return {"access_token": token, "quota": quota, "status": status, **extra}


def snap(quota=0):
    return service.summarize([remote(quota=quota)] if quota else [])


def account(config=None, *, token="at", web=True, imported=False, extra=None):
    config = config or settings()
    payload = {"access_token": token, **(extra or {})}
    if web:
        payload.update({"account_purpose": WEB_POOL_PURPOSE, "chatgpt_web_only": True, "chatgpt_web_pool_target": config.target_id})
    if imported:
        payload["sync_statuses"] = {"chatgpt2api": {"imported": True}}
    with Session(service.engine) as session:
        row = db.AccountModel(platform="chatgpt", email=f"{token}@example.invalid", password="local-secret", token=token)
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


def run(config=None, register=None, control=None, should_continue=None):
    return service.replenish(
        config or settings(), control=control or RegisterTaskControl(), log=lambda _: None,
        register=register or Mock(return_value=None), progress=lambda _: None,
        should_continue=should_continue or (lambda: True),
    )


def test_config_default_off_and_secret_not_public():
    assert not service.WebPoolSettings.from_store().enabled
    value = settings()
    service.save_settings(value)
    assert service.WebPoolSettings.from_store() == value
    assert "test-admin-secret" not in str(value.public_dict())
    assert "test-admin-secret" not in repr(value)
    assert value.public_dict()["admin_token_configured"]


@pytest.mark.parametrize("change", [
    {"base_url": "file:///tmp/key"}, {"base_url": "https://user:secret@pool.test"},
    {"base_url": "https://pool.test?key=secret"}, {"base_url": "https://pool.test/api/accounts"},
    {"admin_token": "key\nother"}, {"target_value": 0}, {"interval_minutes": 0},
    {"max_attempts_per_run": 0}, {"daily_registration_limit": -1}, {"target_mode": "codex"},
    {"delay_seconds": float("inf")},
])
def test_invalid_settings_rejected(change):
    with pytest.raises(ValueError):
        service.WebPoolSettings(**change)


def test_quota_matches_normal_positive_image_inventory():
    items = [remote(quota=5), remote(quota=-1), remote(quota=0), remote(quota=100, status="限流"), remote(quota=100, status="禁用"), remote(quota=100, status="异常")]
    result = service.summarize(service.parse_items({"items": items}))
    assert result["remaining_quota"] == 5
    assert result["normal_accounts"] == 3
    assert result["available_accounts"] == 1
    assert result["total_accounts"] == 6
    assert service.target_reached(settings(target_mode="available_accounts", target_value=1), result)
    assert not service.target_reached(settings(target_mode="quota", target_value=6), result)


@pytest.mark.parametrize("data", [
    {}, {"items": None}, {"items": [{}]}, {"items": [remote(status="unknown")]},
    {"items": [remote(quota=None)]}, {"items": [remote(quota=True)]},
    {"items": [remote(quota="NaN")]}, {"items": [remote(quota=1.5)]},
    {"items": [remote(image_quota_unknown=True)]},
])
def test_unknown_or_invalid_quota_is_not_zero(data):
    with pytest.raises(service.PoolError):
        service.parse_items(data)


def test_http_uses_admin_contract_and_no_redirect_or_environment(monkeypatch):
    seen = {}
    def request(session, method, url, **kwargs):
        seen.update({"trust_env": session.trust_env, "method": method, "url": url, **kwargs})
        return SimpleNamespace(status_code=200, json=lambda: {"items": [remote()]})
    monkeypatch.setattr(requests.Session, "request", request)
    result = service.check_quota(settings())
    assert result["remaining_quota"] == 5
    assert seen["url"] == "https://pool.example.invalid/prefix/api/accounts"
    assert seen["method"] == "GET"
    assert seen["headers"]["Authorization"] == "Bearer test-admin-secret"
    assert seen["allow_redirects"] is False
    assert seen["trust_env"] is False
    assert "access_token" not in str(service.last_snapshot(settings()))


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_error_never_registers(monkeypatch, status):
    monkeypatch.setattr(requests.Session, "request", Mock(return_value=SimpleNamespace(status_code=status)))
    register = Mock()
    with pytest.raises(service.PoolError):
        run(register=register)
    register.assert_not_called()
    assert service.daily_used() == 0


def test_timeout_redacts_exception_url_and_key(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=requests.Timeout("secret-url?key=actual-key")))
    with pytest.raises(service.PoolError) as error:
        service.check_quota(settings())
    assert "actual-key" not in str(error.value)


def test_html_login_is_not_success(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", Mock(return_value=SimpleNamespace(status_code=200, json=Mock(side_effect=ValueError("html")))))
    with pytest.raises(service.PoolError):
        service.check_quota(settings())


def test_upload_sends_only_at_email_and_verifies_exact_account(monkeypatch):
    row = account(extra={"refresh_token": "mail-rt", "session_token": "st", "cookies": "cookie"})
    response = {"added": 1, "skipped": 0, "refreshed": 1, "errors": [], "items": [remote()]}
    request = Mock(return_value=response)
    pool = service.WebPoolClient(settings())
    monkeypatch.setattr(pool, "_request", request)
    result = pool.upload(row)
    assert result["ready"]
    request.assert_called_once_with("POST", {"accounts": [{"access_token": "at", "email": row.email}]})


@pytest.mark.parametrize("response", [
    {"added": 1},
    {"added": 1, "skipped": 0, "refreshed": 1, "errors": [], "items": [remote(token="other")]},
    {"added": 1, "skipped": 0, "refreshed": 1, "errors": [], "items": [remote(quota=None)]},
])
def test_upload_200_does_not_mean_confirmed(monkeypatch, response):
    pool = service.WebPoolClient(settings())
    monkeypatch.setattr(pool, "_request", Mock(return_value=response))
    with pytest.raises(service.PoolError):
        pool.upload(account())


@pytest.mark.parametrize("quota,status,errors", [(0, "正常", []), (5, "异常", []), (5, "正常", [{"error": "bad"}])])
def test_upload_imported_is_not_always_usable(monkeypatch, quota, status, errors):
    pool = service.WebPoolClient(settings())
    monkeypatch.setattr(pool, "_request", Mock(return_value={"added": 1, "skipped": 0, "refreshed": 0, "errors": errors, "items": [remote(quota=quota, status=status)]}))
    result = pool.upload(account())
    assert result["imported"]
    assert not result["ready"]


def test_sufficient_pool_skips_without_spending(monkeypatch):
    monkeypatch.setattr(service.WebPoolClient, "snapshot", Mock(return_value=snap(100)))
    register = Mock()
    result = run(register=register)
    assert result.attempts == 0
    register.assert_not_called()
    assert service.daily_used() == 0


def test_pending_upload_first_then_register_until_rechecked_target(monkeypatch):
    old = account(token="existing")
    state = {"quota": 0}
    order = []
    monkeypatch.setattr(service.WebPoolClient, "snapshot", lambda _: snap(state["quota"]))
    def upload(_client, row):
        order.append(row.id)
        state["quota"] += 5
        return {"imported": True, "ready": True, "quota": 5, "status": "正常", "refresh_errors": 0}
    monkeypatch.setattr(service.WebPoolClient, "upload", upload)
    def register(config, _control):
        order.append("register")
        return account(config, token="new").id
    result = run(settings(target_value=10), register=register)
    assert order[:2] == [old.id, "register"]
    assert result.success == 2
    assert result.registrations == 1
    assert result.snapshot["remaining_quota"] == 10
    assert service.pending_count(settings()) == 0
    assert service.daily_used() == 1


def test_concurrent_remote_fill_keeps_new_account_local(monkeypatch):
    monkeypatch.setattr(service.WebPoolClient, "snapshot", Mock(side_effect=[snap(), snap(100)]))
    upload = Mock()
    monkeypatch.setattr(service.WebPoolClient, "upload", upload)
    result = run(register=lambda config, _: account(config).id)
    assert result.registrations == 1
    assert result.success == 0
    assert service.pending_count(settings()) == 1
    upload.assert_not_called()


def test_upload_timeout_stops_new_registration_and_retries_same_local_account(monkeypatch):
    monkeypatch.setattr(service.WebPoolClient, "find_imported", Mock(return_value=None))
    monkeypatch.setattr(service.WebPoolClient, "snapshot", Mock(return_value=snap()))
    monkeypatch.setattr(service.WebPoolClient, "upload", Mock(side_effect=service.PoolError("上传未确认")))
    register = Mock(side_effect=lambda config, _: account(config).id)
    first = run(register=register)
    second = run(register=register)
    assert first.registrations == 1
    assert second.registrations == 0
    assert register.call_count == 1
    assert service.pending_count(settings()) == 1
    assert first.errors and second.errors


def test_max_failures_per_run_and_daily_budget(monkeypatch):
    monkeypatch.setattr(service.WebPoolClient, "snapshot", Mock(return_value=snap()))
    config = settings(max_attempts_per_run=8, max_consecutive_failures=2, daily_registration_limit=3)
    register = Mock(return_value=None)
    first = run(config, register)
    second = run(config, register)
    assert first.registrations == 2
    assert second.registrations == 1
    assert register.call_count == 3
    assert service.daily_used() == 3


def test_zero_daily_limit_round_trips_and_keeps_usage_counting(isolated):
    config = settings(daily_registration_limit=0)
    service.save_settings(config)
    assert service.WebPoolSettings.from_store().daily_registration_limit == 0
    assert isolated[service.PREFIX + "daily_registration_limit"] == "0"
    isolated.update({service.PREFIX + "budget_date": service.today(), service.PREFIX + "budget_used": "1500"})
    for _ in range(3):
        assert service.reserve_registration(config)
    assert service.daily_used() == 1503
    # Switching back to a finite limit must not reset today's usage.
    assert not service.reserve_registration(settings(daily_registration_limit=500))


def test_zero_daily_limit_api_status_is_unlimited_not_exhausted():
    config = settings(daily_registration_limit=0)
    response = client().put("/chatgpt-web-pool/config", json={"config": config.model_dump()})
    assert response.status_code == 200
    assert response.json()["config"]["daily_registration_limit"] == 0
    assert service.reserve_registration(config)
    status = client().get("/chatgpt-web-pool/status").json()
    assert status["daily_registration_unlimited"] is True
    assert status["daily_registrations_remaining"] is None
    assert status["daily_registrations_used"] == 1
    assert tasks._task_store.list_snapshots() == []


def test_finite_daily_limit_status_still_reports_remaining():
    config = settings(daily_registration_limit=2)
    service.save_settings(config)
    assert service.reserve_registration(config)
    status = client().get("/chatgpt-web-pool/status").json()
    assert status["daily_registration_unlimited"] is False
    assert status["daily_registrations_remaining"] == 1


@pytest.mark.parametrize("max_attempts,max_failures,expected", [(2, 3, 2), (8, 2, 2)])
def test_unlimited_daily_limit_keeps_per_run_and_failure_bounds(monkeypatch, isolated, max_attempts, max_failures, expected):
    isolated.update({service.PREFIX + "budget_date": service.today(), service.PREFIX + "budget_used": "5000"})
    monkeypatch.setattr(service.WebPoolClient, "snapshot", Mock(return_value=snap()))
    register = Mock(return_value=None)
    result = run(settings(daily_registration_limit=0, max_attempts_per_run=max_attempts,
                          max_consecutive_failures=max_failures), register)
    assert result.registrations == expected
    assert register.call_count == expected
    assert service.daily_used() == 5000 + expected
    assert "每日" not in result.reason


def test_unlimited_daily_limit_still_requires_email_cost_confirmation():
    config = settings(enabled=True, daily_registration_limit=0)
    response = client().put("/chatgpt-web-pool/config", json={"config": config.model_dump()})
    assert response.status_code == 400
    assert not service.WebPoolSettings.from_store().enabled


def test_new_utc_day_resets_registration_budget(isolated):
    isolated.update({service.PREFIX + "budget_date": "2000-01-01", service.PREFIX + "budget_used": "999"})
    assert service.daily_used() == 0
    assert service.reserve_registration(settings(daily_registration_limit=1))
    assert not service.reserve_registration(settings(daily_registration_limit=1))


def test_disabled_and_stop_prevent_side_effects(monkeypatch):
    snapshot = Mock(return_value=snap())
    monkeypatch.setattr(service.WebPoolClient, "snapshot", snapshot)
    register = Mock()
    assert run(register=register, should_continue=lambda: False).attempts == 0
    switch = RegisterTaskControl()
    switch.request_stop()
    with pytest.raises(StopTaskRequested):
        run(register=register, control=switch)
    register.assert_not_called()


def test_destination_fingerprint_prevents_cross_target_upload():
    row = account()
    other = settings().model_copy(update={"base_url": "https://other.example.invalid"})
    assert service.pending_count(other) == 0
    with pytest.raises(service.PoolError):
        service.upload_account(row.id, other, Mock())


def test_web_account_available_to_sms_stock_and_backfill_but_not_inspection_or_sync():
    from services.external_sync import sync_account
    from services.chatgpt_rt_backfill import select_backfill_targets
    web = account(token="web")
    normal = account(token="normal", web=False)
    # 接码自动补库现在可以把免接码账号当作可补 RT/接码的候选
    assert sms_stock_service._eligible_status(web) is True
    assert sms_stock_service._pending(web) is True
    assert not sms_stock_service._in_stock(web)  # 还没有 RT，暂不计入库存
    # 批量补 RT 也能选到它（与接码补库同一套 RT 链路）
    with Session(service.engine) as session:
        rows, _ = select_backfill_targets(session, all_filtered=True)
        assert {row.id for row in rows} == {web.id, normal.id}
    # 但仍然不进入 Codex/RT 巡检，也不会自动外传到 CPA/Sub2API
    assert inspection_service.select_account_ids()[0] == [normal.id]
    assert sync_account(web) == []


def test_status_config_are_local_and_check_does_not_create_tasks(monkeypatch):
    service.save_settings(settings())
    snapshot = Mock(return_value=snap(7))
    monkeypatch.setattr(service.WebPoolClient, "snapshot", snapshot)
    assert client().get("/chatgpt-web-pool/config").status_code == 200
    assert client().get("/chatgpt-web-pool/status").status_code == 200
    snapshot.assert_not_called()
    response = client().post("/chatgpt-web-pool/check", json={})
    assert response.json()["snapshot"]["remaining_quota"] == 7
    assert tasks._task_store.list_snapshots() == []
    assert service.daily_used() == 0


def test_enable_and_run_require_email_cost_confirmation():
    response = client().put("/chatgpt-web-pool/config", json={"config": settings(enabled=True).model_dump()})
    assert response.status_code == 400
    assert client().post("/chatgpt-web-pool/run", json={}).status_code == 400
    assert not service.WebPoolSettings.from_store().enabled


def test_blank_token_preserved_and_enable_wakes_once(monkeypatch):
    wake = Mock()
    monkeypatch.setattr("core.scheduler.scheduler.trigger_web_pool_now", wake)
    service.save_settings(settings())
    data = settings(enabled=True).model_dump()
    data["admin_token"] = ""
    for _ in range(2):
        response = client().put("/chatgpt-web-pool/config", json={"config": data, "confirm_registration": True})
        assert response.status_code == 200
        assert "test-admin-secret" not in response.text
    assert service.WebPoolSettings.from_store().admin_token == "test-admin-secret"
    wake.assert_called_once()


def test_disable_and_changed_config_stop_existing_parent():
    service.save_settings(settings(enabled=True))
    tasks._task_store.create("parent", platform="chatgpt", total=1, source=service.SOURCE)
    response = client().put("/chatgpt-web-pool/config", json={"config": settings(enabled=False).model_dump()})
    assert response.status_code == 200
    assert tasks._task_store.control_for("parent").is_stop_requested()


def test_active_tasks_exclude_duplicate_and_conflicting_runs():
    service.save_settings(settings())
    first = api.enqueue(trigger="manual", background_tasks=BackgroundTasks())
    assert first["triggered"]
    with pytest.raises(HTTPException) as error:
        api.enqueue(trigger="manual", background_tasks=BackgroundTasks())
    assert error.value.status_code == 409
    assert client().post("/tasks/register", json={"platform": "chatgpt"}).status_code == 409
    assert client().post("/tasks/backfill-rt", json={"all_filtered": True}).status_code == 409
    from api.inspection import _has_active_inspection_task
    assert _has_active_inspection_task()


def test_register_child_forces_no_sms_rt_and_skips_global_upload(monkeypatch):
    configs = []
    class Platform:
        def __init__(self, config, mailbox):
            configs.append(config.extra)
            self.mailbox = mailbox
        def bind_task_control(self, _control):
            pass
        def register(self, **_kwargs):
            return Account(platform="chatgpt", email="web@example.invalid", password="pw", token="at", extra={"session_token": "st"})
    monkeypatch.setattr("core.registry.get", lambda _: Platform)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda **_: SimpleNamespace())
    parent = tasks._task_store.create("parent", platform="chatgpt", total=1, source=service.SOURCE)
    row_id = api._register_one("parent", settings(), parent.control)
    assert row_id
    assert configs[0]["sms_enabled"] == "0"
    assert configs[0]["chatgpt_register_flow"] == "email"
    assert configs[0]["chatgpt_registration_mode"] == "access_token_only"
    assert configs[0]["chatgpt_require_rt_ready"] == "0"
    assert configs[0]["chatgpt_bind_2fa"] == "0"
    tasks._auto_upload_integrations.assert_not_called()
    fields = tasks._load_account_fields(row_id)
    assert fields["extra"]["account_purpose"] == WEB_POOL_PURPOSE
    assert fields["extra"]["chatgpt_web_pool_target"] == settings().target_id
    child_id = tasks._task_store.snapshot("parent")["meta"]["child_task_ids"][0]
    assert tasks._task_store.control_for(child_id) is parent.control


def test_manual_run_summary_persisted_without_secret(monkeypatch):
    service.save_settings(settings())
    monkeypatch.setattr(service.WebPoolClient, "snapshot", Mock(return_value=snap(100)))
    response = client().post("/chatgpt-web-pool/run", json={"confirm_registration": True})
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    result = client().get(f"/tasks/{task_id}").json()
    assert result["status"] == "done"
    assert result["meta"]["snapshot"]["remaining_quota"] == 100
    assert "test-admin-secret" not in str(result)


def test_scheduler_independent_default_and_interval():
    from core.scheduler import Scheduler
    scheduler = Scheduler()
    assert scheduler._get_web_pool_interval_seconds() == 0
    service.save_settings(settings(enabled=True, interval_minutes=7))
    assert scheduler._get_web_pool_interval_seconds() == 420
    assert scheduler._get_sms_stock_interval_seconds() == 0
    assert scheduler._get_inspection_interval_seconds() == 0
