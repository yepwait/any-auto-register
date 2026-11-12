"""Real middleware/queue regressions: no external network or paid operations."""
import asyncio
import json
import time
from unittest.mock import Mock, patch

import pytest
import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from api import chatgpt_web_pool as api, tasks
from core import db
from core.task_runtime import RegisterTaskStore
from services import chatgpt_web_pool as pool, web_pool_events as events


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    for module in (pool, tasks, db):
        monkeypatch.setattr(module, "engine", engine)
    saved = {events.SECRET_KEY: "test-shared-key", "auth_password_hash": "configured"}
    monkeypatch.setattr(pool.config_store, "get", lambda key, default="": saved.get(key, default))
    monkeypatch.setattr(pool.config_store, "get_all", lambda: dict(saved))
    monkeypatch.setattr(pool.config_store, "set_many", saved.update)
    monkeypatch.setattr(tasks, "_task_store", RegisterTaskStore())
    monkeypatch.setattr(tasks, "_task_admission_closed", False)
    monkeypatch.setattr(tasks, "_save_task_log", Mock())
    monkeypatch.setattr(tasks, "_auto_upload_integrations", Mock())
    monkeypatch.setattr(events, "rate_limit", events.TriggerRateLimit())
    monkeypatch.setattr(events, "wake", Mock())
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("network forbidden")))
    yield saved
    engine.dispose()


def config(**changes):
    value = pool.WebPoolSettings(base_url="https://pool.example.invalid", admin_token="test-admin", delay_seconds=0, **changes)
    pool.save_settings(value)
    return value


def client():
    # Import our application, with the REAL authentication middleware. Do not
    # start lifespan/scheduler; use a small app to keep all execution offline.
    from main import auth_middleware
    app = FastAPI()
    app.middleware("http")(auth_middleware)
    app.include_router(api.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    return TestClient(app)


def post(data=None, headers=None):
    return client().post("/api/chatgpt-web-pool/trigger", json=data or {},
                         headers=headers if headers is not None else {"X-Replenish-Secret": "test-shared-key"})


def snap(value=0):
    return pool.summarize([{"status": "正常", "quota": value}] if value else [])


def run_pending():
    bg = BackgroundTasks()
    result = api.dispatch_pending_trigger(background_tasks=bg)
    asyncio.run(bg())
    return result


def test_unconfigured_secret_fails_closed_even_with_panel_disabled(isolated):
    config(enabled=True)
    isolated[events.SECRET_KEY] = ""
    isolated["auth_password_hash"] = ""
    response = post(headers={})
    assert response.status_code == 503
    assert events.STATE_KEY not in isolated
    assert tasks._task_store.list_snapshots() == []


@pytest.mark.parametrize("headers,body", [({}, {}), ({"X-Replenish-Secret": "wrong"}, {}),
    ({"X-Replenish-Secret": "test-shared-key"}, {"secret": "other"})])
def test_invalid_auth_has_no_queue_or_network_side_effect(isolated, headers, body):
    config(enabled=True)
    assert post(body, headers).status_code == 401
    assert events.STATE_KEY not in isolated
    requests.Session.request.assert_not_called()


def test_body_secret_remains_supported_but_neighbor_routes_require_panel_jwt():
    config(enabled=True)
    result = post({"secret": "test-shared-key"}, headers={})
    assert result.status_code == 202
    assert result.json()["accepted"] is True
    assert client().get("/api/chatgpt-web-pool/config").status_code == 401
    assert client().get("/api/chatgpt-web-pool/trigger").status_code == 401
    assert client().post("/api/chatgpt-web-pool/run", json={"confirm_registration": True}).status_code == 401


def test_disabled_pool_acknowledges_without_spending_or_saving_queue(isolated):
    config(enabled=False)
    response = post()
    assert response.status_code == 202
    assert response.json()["accepted"] is False
    assert events.STATE_KEY not in isolated


def test_payload_quota_and_threshold_cannot_override_saved_policy(isolated):
    settings = config(enabled=True, target_value=100)
    response = post({"total_quota": -999999, "threshold": 9999999, "url": "https://evil.invalid", "event_id": "e-1"})
    assert response.status_code == 202
    assert response.json()["queued"] is True
    assert tasks._task_store.list_snapshots() == []  # ACK never starts per-event tasks
    stored = isolated[events.STATE_KEY]
    assert "test-shared-key" not in stored and "evil" not in stored and "test-admin" not in stored
    assert events.pending_state(settings)["pending"]


def test_duplicate_events_are_bounded_and_coalesced_without_registration(isolated):
    settings = config(enabled=True)
    for _ in range(5):
        response = post({"event_id": "same-id"})
        assert response.status_code == 202
    assert events.status()["received"] == 1
    assert response.json()["duplicate"] is True
    for index in range(200):
        events.accept(settings, event_id=f"e-{index}")
    state = json.loads(isolated[events.STATE_KEY])
    assert len(state["recent_events"]) <= events.MAX_RECENT_EVENTS
    assert state["pending"] is True and state["coalesced"] == 200
    assert tasks._task_store.list_snapshots() == []


def test_fixed_rate_limit_returns_retry_after_and_retains_existing_wakeup():
    config(enabled=True)
    response = None
    for _ in range(25):
        response = post()
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    assert events.status()["pending"] is True


def test_stale_event_and_large_request_rejected_before_work():
    config(enabled=True)
    assert post({"event_id": "old", "sent_at": time.time() - 3600}).status_code == 400
    assert post({"secret": "x" * 5000}).status_code == 413
    assert tasks._task_store.list_snapshots() == []


def test_busy_event_is_not_lost_and_runs_after_busy_task_finishes(monkeypatch):
    settings = config(enabled=True)
    tasks._task_store.create("busy", platform="chatgpt", total=1, source="inspection")
    assert post({"event_id": "e1"}).status_code == 202
    assert api.dispatch_pending_trigger()["queued"]
    assert events.status()["status"] == "waiting_busy"
    tasks._task_store.finish("busy", status="done", success=0, skipped=0, errors=[])
    monkeypatch.setattr(pool.WebPoolClient, "snapshot", Mock(return_value=snap(200)))
    register = Mock(side_effect=AssertionError("already sufficient"))
    monkeypatch.setattr(api, "_register_one", register)
    result = run_pending()
    assert result["triggered"] is True
    assert events.status()["status"] == "idle"
    assert events.status()["pending"] is False
    register.assert_not_called()
    assert pool.daily_used() == 0


def test_restart_recovers_persisted_notification_without_in_memory_queue(monkeypatch):
    settings = config(enabled=True)
    events.accept(settings, event_id="persisted")
    assert events.claim("crashed", settings)
    # A fresh process has no in-memory task; persisted wake survives.
    monkeypatch.setattr(pool.WebPoolClient, "snapshot", Mock(return_value=snap(200)))
    assert run_pending()["triggered"]
    assert events.status()["pending"] is False


def test_new_event_during_processing_is_not_lost_on_completion():
    settings = config(enabled=True)
    events.accept(settings, event_id="first")
    assert events.claim("active", settings)
    events.accept(settings, event_id="second")
    events.complete("active", reached=True, reason="target reached")
    assert events.status()["pending"] is True
    assert events.status()["status"] == "pending"


def test_changed_destination_or_disabled_configuration_invalidates_old_pending():
    settings = config(enabled=True)
    events.accept(settings)
    other = settings.model_copy(update={"base_url": "https://different.invalid"})
    assert events.pending_state(other) is None
    assert events.status()["pending"] is False
    events.accept(settings)
    assert events.pending_state(settings.model_copy(update={"enabled": False})) is None


def test_rotating_trigger_key_revokes_queued_events(isolated):
    settings = config(enabled=True)
    events.accept(settings, event_id="authorized-with-old-key")
    isolated[events.SECRET_KEY] = "new-trigger-key"
    assert events.pending_state(settings) is None
    assert not events.status()["pending"]
    assert events.status()["status"] == "paused"
    assert "new-trigger-key" not in isolated[events.STATE_KEY]


def test_errors_back_off_without_unbounded_self_retry():
    settings = config(enabled=True)
    events.accept(settings)
    for index in range(events.MAX_RETRY_RUNS):
        with patch.object(events.time, "time", return_value=time.time() + index * 1000):
            assert events.claim(f"try-{index}", settings)
            events.complete(f"try-{index}", reached=False, failed=True, reason="network failed")
    state = events.status()
    assert state["pending"] is False
    assert state["status"] == "error"
    assert state["failure_runs"] == events.MAX_RETRY_RUNS


def test_per_run_limit_keeps_need_pending_but_respects_short_cooldown():
    settings = config(enabled=True)
    events.accept(settings)
    assert events.claim("batch", settings)
    events.complete("batch", reached=False, retry=True, refill_latched=True, reason="per-run limit")
    state = events.status()
    assert state["pending"] and state["refill_latched"]
    assert events.pending_state(settings) is None


def test_daily_budget_waits_until_next_utc_day():
    settings = config(enabled=True)
    events.accept(settings)
    assert events.claim("budget", settings)
    events.complete("budget", reached=False, budget_exhausted=True)
    state = events.status()
    assert state["status"] == "cooldown" and state["pending"]
    assert state["next_attempt_at"] > time.time()


def test_capacity_and_maintenance_do_not_drop_pending(monkeypatch):
    settings = config(enabled=True)
    events.accept(settings)
    monkeypatch.setattr(tasks, "ensure_task_capacity", Mock(side_effect=HTTPException(503, "maintenance")))
    assert api.dispatch_pending_trigger()["queued"]
    assert events.status()["pending"]


def test_submission_failure_keeps_bounded_retry(monkeypatch):
    settings = config(enabled=True)
    events.accept(settings)
    monkeypatch.setattr(api, "enqueue", Mock(side_effect=RuntimeError("thread failed")))
    assert api.dispatch_pending_trigger()["queued"]
    state = events.status()
    assert state["pending"] and not state["active_task_id"]
    assert state["status"] == "cooldown"


def test_readonly_status_never_enqueues_or_exposes_secret(isolated):
    settings = config(enabled=True)
    events.accept(settings)
    value = api.get_status()
    assert value["realtime"]["secret_configured"]
    assert "test-shared-key" not in str(value)
    assert "test-admin" not in str(value)
    assert tasks._task_store.list_snapshots() == []


def test_duplicate_after_completion_does_not_create_new_work():
    settings = config(enabled=True)
    events.accept(settings, event_id="once")
    assert events.claim("done", settings)
    events.complete("done", reached=True)
    result = events.accept(settings, event_id="once")
    assert result["duplicate"] and not result["queued"]


def test_new_events_during_failure_cannot_bypass_circuit_limit():
    settings = config(enabled=True)
    now = time.time()
    events.accept(settings)
    for index in range(events.MAX_RETRY_RUNS):
        with patch.object(events.time, "time", return_value=now + index * 1000):
            assert events.claim(f"flood-{index}", settings)
            events.accept(settings, event_id=f"during-{index}")
            events.complete(f"flood-{index}", reached=False, failed=True)
    assert events.status()["pending"] is False
    assert events.status()["status"] == "error"


def test_enqueue_initial_save_error_does_not_leave_immortal_pending_task(monkeypatch):
    settings = config(enabled=True)
    events.accept(settings)
    monkeypatch.setattr(tasks, "_persist_task_snapshot", Mock(side_effect=RuntimeError("db unavailable")))
    assert api.dispatch_pending_trigger()["queued"]
    assert all(item["status"] == "failed" for item in tasks._task_store.list_snapshots())
    assert not tasks.has_active_register_task(platform="chatgpt")
    assert events.status()["status"] == "cooldown"


def test_revoking_secret_during_run_prevents_next_registration(monkeypatch, isolated):
    config(enabled=True)
    post({"event_id": "revoked"})
    def revoke(_client):
        isolated[events.SECRET_KEY] = ""
        return snap(0)
    monkeypatch.setattr(pool.WebPoolClient, "snapshot", revoke)
    register = Mock(side_effect=AssertionError("no registration after revoke"))
    monkeypatch.setattr(api, "_register_one", register)
    run_pending()
    register.assert_not_called()
    assert pool.daily_used() == 0
    assert events.status()["status"] == "paused"


def test_failed_recovery_keeps_refill_latch():
    settings = config(enabled=True)
    events.accept(settings)
    assert events.claim("failed", settings)
    state = events._load()
    state["refill_latched"] = True
    events._save(state)
    tasks._task_store.create("failed", platform="chatgpt", total=1, source=pool.SOURCE)
    tasks._task_store.finish("failed", status="failed", success=0, skipped=0, errors=["offline"])
    api.dispatch_pending_trigger()
    assert events.status()["refill_latched"] is True


def test_chunked_oversized_body_rejected_without_content_length():
    config(enabled=True)
    body = [b'{"secret":"test-shared-key","padding":"', b"a" * 5000, b'"}']
    response = client().post("/api/chatgpt-web-pool/trigger", content=iter(body),
                             headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert tasks._task_store.list_snapshots() == []


def test_transient_read_error_retains_bounded_pending_for_retry(monkeypatch):
    config(enabled=True)
    post({"event_id": "check-error"})
    monkeypatch.setattr(pool.WebPoolClient, "snapshot", Mock(side_effect=pool.PoolError("offline")))
    run_pending()
    assert events.status()["status"] == "cooldown"
    assert events.status()["pending"]
    assert pool.daily_used() == 0
