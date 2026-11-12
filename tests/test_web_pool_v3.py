"""Offline yukkcat v3 contract tests; no remote project or user database is used.

The projections below mirror the confirmed management API. In particular, v3
never echoes an access token, uses backend_status/available for account state,
and reports operation completion with a real boolean done field.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core import db
from core.task_runtime import RegisterTaskControl
from services import chatgpt_web_pool as service
from services.chatgpt_account_purpose import WEB_POOL_PURPOSE, credential_fingerprint


BASE_URL = "https://pool.example.invalid/management"
ADMIN_TOKEN = "offline-admin-token"
REMOTE_ID = "acct_saved"
_DEFAULT = object()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(service, "engine", engine)
    monkeypatch.setattr(db, "engine", engine)
    saved = {}
    monkeypatch.setattr(service.config_store, "get", lambda key, default="": saved.get(key, default))
    monkeypatch.setattr(service.config_store, "get_all", lambda: dict(saved))
    monkeypatch.setattr(service.config_store, "set_many", saved.update)
    monkeypatch.setattr(requests.Session, "request", Mock(side_effect=AssertionError("real HTTP forbidden")))
    yield saved
    engine.dispose()


def settings(**changes):
    return service.WebPoolSettings(**{
        "base_url": BASE_URL, "admin_token": ADMIN_TOKEN, "delay_seconds": 0,
        "request_timeout": 5, **changes,
    })


def v3_item(remote_id="acct_new", *, quota=5, available=True, unknown=False,
            unlimited=False, backend_status="正常", **changes):
    return {
        "id": remote_id, "email": "same@example.invalid", "user_id": "user-1",
        "backend_status": backend_status,
        "status_category": {"正常": "normal", "限流": "limited", "异常": "abnormal", "禁用": "disabled"}[backend_status],
        "available": available, "quota_remaining": quota, "quota_unknown": unknown,
        "quota_unlimited": unlimited,
        "quota_state": "unlimited" if unlimited else "unknown" if unknown else "known",
        **changes,
    }


def v3_page(items=(), *, total=None, page=1, page_size=500, **changes):
    total = len(items) if total is None else total
    return {"items": list(items), "total": total, "all_total": total,
            "page": page, "page_size": page_size, **changes}


def imported(item=None, **changes):
    item = v3_item() if item is None else item
    return {"added": 1, "skipped": 0, "synced": 1, "updated_ids": [item["id"]],
            "removed_ids": [], "errors": [], "events": [], "items": [item], **changes}


def updated(item=None, **changes):
    item = v3_item(REMOTE_ID) if item is None else item
    return {"updated": 1, "removed": 0, "updated_ids": [item["id"]],
            "removed_ids": [], "errors": [], "events": [], "items": [item],
            "item": item, **changes}


def operation(*, done=True, stage=None, result=_DEFAULT, **changes):
    return {
        "total": 1, "processed": 1 if done is True else 0, "done": done,
        "stage": stage or ("completed" if done is True else "syncing"),
        "status_counts": {"正常": 1} if done is True else {}, "total_quota": 5,
        "events": [], "error": "",
        "result": (updated() if done is True else None) if result is _DEFAULT else result,
        **changes,
    }


def local_account(config=None, *, token="new-web-at", state=None, email="same@example.invalid"):
    config = config or settings()
    extra = {
        "access_token": token, "refresh_token": "local-chatgpt-rt",
        "mail_refresh_token": "local-mailbox-rt", "mail_password": "local-mailbox-password",
        "session_token": "local-session", "cookies": "local-cookie",
        "account_purpose": WEB_POOL_PURPOSE, "chatgpt_web_only": True,
        "chatgpt_web_pool_target": config.target_id,
    }
    if state is not None:
        extra["sync_statuses"] = {"chatgpt2api": deepcopy(state)}
    with Session(service.engine) as session:
        row = db.AccountModel(platform="chatgpt", email=email, password="local-account-password",
                              user_id="user-1", token=token, status="registered")
        row.set_extra(extra)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def resync_account(config=None, **changes):
    state = {
        "remote_id": REMOTE_ID, "protocol": "yukkcat_v3", "imported": True,
        "ready": False, "needs_resync": True, "uncertain": False,
        "credential_fingerprint": credential_fingerprint("previous-web-at"),
        **changes,
    }
    return local_account(config, state=state)


def sync_state(account_id):
    with Session(service.engine) as session:
        return session.get(db.AccountModel, account_id).get_extra()["sync_statuses"]["chatgpt2api"]


def mock_http(monkeypatch, handler):
    calls = []

    def request(session, method, url, **kwargs):
        assert url.startswith(BASE_URL + "/api/accounts")
        assert session.trust_env is False
        assert kwargs["allow_redirects"] is False
        assert kwargs["headers"]["Authorization"] == f"Bearer {ADMIN_TOKEN}"
        # Each request must remain within the configured timeout; polling may
        # shorten it to the remaining overall operation deadline.
        assert 0 < kwargs["timeout"] <= 5
        parts = urlsplit(url)
        call = {"method": method, "path": parts.path.removeprefix("/management"),
                "query": parse_qs(parts.query), "payload": deepcopy(kwargs.get("json"))}
        calls.append(call)
        value = handler(call)
        if isinstance(value, Exception):
            raise value
        status, body = value if isinstance(value, tuple) else (200, value)
        return SimpleNamespace(status_code=status, json=lambda: deepcopy(body))

    monkeypatch.setattr(requests.Session, "request", request)
    return calls


def scripted_http(monkeypatch, responses):
    steps = iter(responses)

    def handler(call):
        method, path, response = next(steps)
        assert (call["method"], call["path"]) == (method, path)
        return response

    return mock_http(monkeypatch, handler)


def install_clock(monkeypatch):
    # Replace only the service's time reference, not Python's shared time module.
    # monotonic itself does not advance wall time; only the mocked polling
    # sleep does. Deadline checks may read it several times per request.
    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    clock.sleep = Mock(side_effect=lambda seconds: setattr(clock, "now", clock.now + seconds))
    monkeypatch.setattr(service, "time", clock)
    return clock


def resync_http(monkeypatch, *, update=_DEFAULT, sync=_DEFAULT, progresses=None,
                detail=_DEFAULT, inventory=None):
    install_clock(monkeypatch)
    update = updated() if update is _DEFAULT else update
    sync = {"progress_id": "op_1", "target_ids": [REMOTE_ID], "errors": []} if sync is _DEFAULT else sync
    progresses = [operation()] if progresses is None else progresses
    detail = {"item": v3_item(REMOTE_ID)} if detail is _DEFAULT else detail
    progress_reads = 0

    def handler(call):
        nonlocal progress_reads
        pair = call["method"], call["path"]
        if pair == ("GET", "/api/accounts"):
            return v3_page(inventory if inventory is not None else [v3_item(REMOTE_ID)])
        if pair == ("POST", "/api/accounts/update"):
            assert call["payload"] == {"id": REMOTE_ID, "access_token": "new-web-at"}
            return update
        if pair == ("POST", "/api/accounts/sync"):
            assert call["payload"] == {"account_ids": [REMOTE_ID]}
            return sync
        if pair == ("GET", "/api/accounts/operations/op_1"):
            value = progresses[min(progress_reads, len(progresses) - 1)]
            progress_reads += 1
            return value
        if pair == ("GET", f"/api/accounts/{REMOTE_ID}"):
            return detail
        pytest.fail(f"unexpected request, including unsafe create during resync: {pair}")

    return mock_http(monkeypatch, handler)


def run(config, register=None):
    return service.replenish(
        config, control=RegisterTaskControl(), log=lambda _: None,
        register=register if register is not None else Mock(side_effect=AssertionError("real registration forbidden")),
        progress=lambda _: None, should_continue=lambda: True,
    )


def assert_pending_resync(row, config):
    state = sync_state(row.id)
    assert state["imported"] is True
    assert state["remote_id"] == REMOTE_ID
    assert state["needs_resync"] is True
    assert state["ready"] is False
    assert state["credential_fingerprint"] == credential_fingerprint("previous-web-at")
    assert service.pending_count(config) == 1


@pytest.mark.parametrize("protocol", ["auto", "legacy", "yukkcat_v3"])
def test_protocol_setting_round_trips_without_exposing_admin_token(protocol):
    config = settings(protocol=protocol)
    service.save_settings(config)
    assert service.WebPoolSettings.from_store().protocol == protocol
    assert config.public_dict()["protocol"] == protocol
    assert ADMIN_TOKEN not in str(config.public_dict())


@pytest.mark.parametrize("is_v3", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_auto_detects_both_protocols_including_empty_inventory(monkeypatch, is_v3, empty):
    data = v3_page([] if empty else [v3_item()]) if is_v3 else {
        "items": [] if empty else [{"status": "正常", "quota": 5, "access_token": "old-at"}],
    }
    scripted_http(monkeypatch, [("GET", "/api/accounts", data)])
    result = service.check_quota(settings())
    assert result["protocol"] == ("yukkcat_v3" if is_v3 else "legacy")
    assert result["total_accounts"] == (0 if empty else 1)
    assert result["remaining_quota"] == (0 if empty else 5)


@pytest.mark.parametrize("protocol,data", [
    ("legacy", v3_page([v3_item()])),
    ("yukkcat_v3", {"items": [{"status": "正常", "quota": 5, "access_token": "legacy-at"}]}),
])
def test_explicit_protocol_mismatch_fails_before_registration(monkeypatch, protocol, data):
    scripted_http(monkeypatch, [("GET", "/api/accounts", data)])
    register = Mock()
    with pytest.raises(service.PoolError):
        run(settings(protocol=protocol), register)
    register.assert_not_called()
    assert service.daily_used() == 0


def test_v3_backend_status_and_available_override_legacy_fields_and_quota(monkeypatch):
    items = [
        v3_item("acct_zero", quota=0, available=True, status="禁用"),
        v3_item("acct_blocked", quota=100, available=False, status="正常"),
        v3_item("acct_limited", quota=100, available=False, backend_status="限流"),
        v3_item("acct_abnormal", quota=100, available=False, backend_status="异常"),
        v3_item("acct_disabled", quota=100, available=False, backend_status="禁用"),
    ]
    scripted_http(monkeypatch, [("GET", "/api/accounts", v3_page(items))])
    snapshot = service.check_quota(settings())
    assert snapshot["normal_accounts"] == 2
    assert snapshot["available_accounts"] == 1
    assert snapshot["remaining_quota"] == 0
    assert snapshot["limited_accounts"] == snapshot["abnormal_accounts"] == snapshot["disabled_accounts"] == 1


def test_all_pages_contribute_to_snapshot_and_cached_summary(monkeypatch):
    calls = scripted_http(monkeypatch, [
        ("GET", "/api/accounts", v3_page([v3_item("acct_1", quota=2), v3_item("acct_2", quota=3)], total=5, page_size=2)),
        ("GET", "/api/accounts", v3_page([v3_item("acct_3", quota=7), v3_item("acct_4", quota=11)], total=5, page=2, page_size=2)),
        ("GET", "/api/accounts", v3_page([v3_item("acct_5", quota=13)], total=5, page=3, page_size=2)),
    ])
    config = settings()
    snapshot = service.check_quota(config)
    assert snapshot["total_accounts"] == snapshot["available_accounts"] == 5
    assert snapshot["remaining_quota"] == 36
    assert [call["query"] for call in calls[1:]] == [
        {"page": ["2"], "page_size": ["2"]}, {"page": ["3"], "page_size": ["2"]},
    ]
    assert service.last_snapshot(config) == snapshot
    assert "access_token" not in str(service.last_snapshot(config))


@pytest.mark.parametrize("bad_page", [
    pytest.param(v3_page([v3_item("acct_1")], total=2, page=2, page_size=1), id="duplicate-id"),
    pytest.param(v3_page([v3_item("acct_2")], total=3, page=2, page_size=1), id="changed-total"),
    pytest.param(v3_page([v3_item("acct_2")], total=2, page=2, page_size=1, all_total=3), id="changed-all-total"),
    pytest.param(v3_page([v3_item("acct_2")], total=2, page=2, page_size=2), id="changed-page-size"),
    pytest.param(v3_page([v3_item("acct_2")], total=2, page=1, page_size=1), id="wrong-page"),
    pytest.param(v3_page([], total=2, page=2, page_size=1), id="early-empty-page"),
    pytest.param(v3_page([v3_item("acct_2"), v3_item("acct_3")], total=2, page=2, page_size=1), id="extra-items"),
    pytest.param({"items": None}, id="bad-items"),
    pytest.param((503, {}), id="later-page-http-error"),
    pytest.param(requests.Timeout("private-upstream-url"), id="later-page-timeout"),
    pytest.param({"items": [], "error": "private-upstream-error"}, id="later-page-business-error"),
])
def test_inconsistent_or_failed_pagination_never_becomes_zero(monkeypatch, bad_page):
    scripted_http(monkeypatch, [
        ("GET", "/api/accounts", v3_page([v3_item("acct_1")], total=2, page_size=1)),
        ("GET", "/api/accounts", bad_page),
    ])
    config = settings(target_value=100)
    register = Mock()
    with pytest.raises(service.PoolError):
        run(config, register)
    register.assert_not_called()
    assert service.daily_used() == 0
    assert service.last_snapshot(config) is None


@pytest.mark.parametrize("changes", [
    {"page_size": 501}, {"page_size": 0}, {"page_size": True}, {"page": 2},
    {"total": -1}, {"total": True}, {"all_total": True},
])
def test_invalid_pagination_metadata_stops_before_registering(monkeypatch, changes):
    scripted_http(monkeypatch, [("GET", "/api/accounts", v3_page([], **changes))])
    register = Mock()
    with pytest.raises(service.PoolError):
        run(settings(), register)
    register.assert_not_called()
    assert service.daily_used() == 0


@pytest.mark.parametrize("changes", [
    {"available": "true"}, {"quota_unknown": 0}, {"quota_unlimited": "false"},
    {"quota_remaining": None}, {"quota_remaining": True}, {"backend_status": "unexpected"}, {"id": ""},
])
def test_invalid_v3_item_never_triggers_registration(monkeypatch, changes):
    item = v3_item()
    item.update(changes)
    scripted_http(monkeypatch, [("GET", "/api/accounts", v3_page([item]))])
    register = Mock()
    with pytest.raises(service.PoolError):
        run(settings(), register)
    register.assert_not_called()


@pytest.mark.parametrize("unknown,unlimited", [(True, False), (False, True), (True, True)])
def test_unknown_and_unlimited_are_explicit_and_block_numeric_refill(monkeypatch, unknown, unlimited):
    item = v3_item(quota=0, unknown=unknown, unlimited=unlimited)
    mock_http(monkeypatch, lambda call: v3_page([item]))
    config = settings(target_mode="quota", target_value=10)
    snapshot = service.check_quota(config)
    assert snapshot["remaining_quota"] is None
    assert snapshot["quota_known"] is False
    assert snapshot["unknown_quota_accounts"] == int(unknown and not unlimited)
    assert snapshot["unlimited_accounts"] == int(unlimited)
    assert snapshot["available_accounts"] == 1
    assert service.last_snapshot(config)["remaining_quota"] is None
    register = Mock()
    with pytest.raises(service.PoolError):
        run(config, register)
    register.assert_not_called()
    assert service.daily_used() == 0


@pytest.mark.parametrize("unknown,unlimited", [(True, False), (False, True), (True, True)])
def test_available_account_target_accepts_unknown_or_unlimited_quota(monkeypatch, unknown, unlimited):
    mock_http(monkeypatch, lambda call: v3_page([v3_item(quota=0, unknown=unknown, unlimited=unlimited)]))
    register = Mock()
    result = run(settings(target_mode="available_accounts", target_value=1), register)
    assert result.attempts == result.registrations == 0
    assert result.snapshot["available_accounts"] == 1
    register.assert_not_called()
    assert service.daily_used() == 0


def test_available_mode_can_import_pending_unknown_quota_account(monkeypatch):
    config = settings(target_mode="available_accounts", target_value=2)
    pending = local_account(config)
    items = [v3_item("acct_existing", quota=0, unlimited=True)]
    calls = []

    def handler(call):
        calls.append(call)
        if call["method"] == "GET":
            return v3_page(items)
        assert call["path"] == "/api/accounts"
        item = v3_item("acct_new", quota=0, unknown=True)
        items.append(item)
        return imported(item)

    mock_http(monkeypatch, handler)
    register = Mock()
    result = run(config, register)
    assert result.success == 1
    assert result.registrations == 0
    assert result.snapshot["available_accounts"] == 2
    assert result.snapshot["remaining_quota"] is None
    assert sync_state(pending.id)["remote_id"] == "acct_new"
    assert service.pending_count(config) == 0
    register.assert_not_called()
    assert len([call for call in calls if call["method"] == "POST"]) == 1


def test_import_sends_only_at_email_and_records_returned_stable_id(monkeypatch):
    config = settings(protocol="yukkcat_v3")
    row = local_account(config)
    calls = scripted_http(monkeypatch, [("POST", "/api/accounts", imported())])
    result = service.upload_account(row.id, config, service.WebPoolClient(config))
    assert calls[0]["payload"] == {
        "accounts": [{"access_token": "new-web-at", "email": row.email}],
        "sync_after_import": True, "return_items": True,
    }
    assert result["ready"] is True
    state = sync_state(row.id)
    assert state["remote_id"] == "acct_new"
    assert state["protocol"] == "yukkcat_v3"
    assert state["credential_fingerprint"] == credential_fingerprint("new-web-at")
    assert state["needs_resync"] is False
    assert state["uncertain"] is False
    assert service.pending_count(config) == 0
    for secret in ("new-web-at", "local-account-password", "local-chatgpt-rt", "local-mailbox-rt", "local-session", "local-cookie"):
        assert secret not in str(state)
    for secret in ("local-account-password", "local-mailbox-password", "local-chatgpt-rt", "local-mailbox-rt", "local-session", "local-cookie"):
        assert secret not in str(calls)


def test_same_email_different_tokens_use_each_mutations_id_projection(monkeypatch):
    config = settings(protocol="yukkcat_v3")
    first = local_account(config, token="first-at")
    second = local_account(config, token="second-at")
    calls = scripted_http(monkeypatch, [
        ("POST", "/api/accounts", imported(v3_item("acct_first"))),
        ("POST", "/api/accounts", imported(v3_item("acct_second"))),
    ])
    client = service.WebPoolClient(config)
    service.upload_account(first.id, config, client)
    service.upload_account(second.id, config, client)
    assert sync_state(first.id)["remote_id"] == "acct_first"
    assert sync_state(second.id)["remote_id"] == "acct_second"
    assert [call["payload"]["accounts"][0]["access_token"] for call in calls] == ["first-at", "second-at"]
    assert service.pending_count(config) == 0


def test_duplicate_token_import_can_confirm_a_skipped_account_without_token_echo(monkeypatch):
    config = settings(protocol="yukkcat_v3")
    row = local_account(config)
    scripted_http(monkeypatch, [("POST", "/api/accounts", imported(added=0, skipped=1))])
    result = service.upload_account(row.id, config, service.WebPoolClient(config))
    assert result["imported"] is True
    assert result["remote_id"] == "acct_new"


@pytest.mark.parametrize("key,bad", [
    ("added", -1), ("added", True), ("skipped", "0"), ("synced", None),
    ("updated_ids", []), ("updated_ids", ["acct_wrong"]), ("removed_ids", ["acct_new"]),
    ("errors", {}), ("events", {}), ("items", [v3_item("acct_wrong")]),
])
def test_malformed_or_unconfirmed_import_retains_local_pending_account(monkeypatch, key, bad):
    config = settings(protocol="yukkcat_v3")
    row = local_account(config)
    response = imported()
    response[key] = bad
    scripted_http(monkeypatch, [("POST", "/api/accounts", response)])
    with pytest.raises(service.PoolError):
        service.upload_account(row.id, config, service.WebPoolClient(config))
    state = sync_state(row.id)
    assert state["imported"] is False
    assert state["ready"] is False
    assert state["uncertain"] is True
    assert service.pending_count(config) == 1


def test_import_with_sync_errors_is_not_counted_as_ready(monkeypatch):
    config = settings(protocol="yukkcat_v3")
    response = imported(errors=[{"id": "acct_new", "error": "refresh failed"}])
    scripted_http(monkeypatch, [("POST", "/api/accounts", response)])
    result = service.upload_account(local_account(config).id, config, service.WebPoolClient(config))
    assert result["ready"] is False


@pytest.mark.parametrize("available,quota", [(True, 0), (False, 100)])
def test_v3_import_readiness_uses_available_and_not_positive_quota(monkeypatch, available, quota):
    config = settings(protocol="yukkcat_v3", target_mode="available_accounts")
    response = imported(v3_item(available=available, quota=quota))
    scripted_http(monkeypatch, [("POST", "/api/accounts", response)])
    result = service.upload_account(local_account(config).id, config, service.WebPoolClient(config))
    assert result["imported"] is True
    assert result["ready"] is available


def test_resync_can_be_stopped_during_progress_without_losing_pending_state(monkeypatch):
    from core.task_runtime import StopTaskRequested
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config)
    calls = resync_http(monkeypatch, progresses=[operation(done=False)])
    client = service.WebPoolClient(config)
    def checkpoint():
        if any(call["path"].startswith("/api/accounts/operations/") for call in calls):
            raise StopTaskRequested()
    client.checkpoint = checkpoint
    with pytest.raises(StopTaskRequested):
        service.upload_account(row.id, config, client)
    assert sync_state(row.id)["needs_resync"]
    assert sync_state(row.id)["uncertain"]
    assert len(calls) == 3
    assert client._operation_deadline is None


def test_resync_updates_saved_id_then_waits_for_boolean_done_and_checks_detail(monkeypatch):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config)
    calls = resync_http(monkeypatch, progresses=[operation(done=False), operation()])
    result = service.upload_account(row.id, config, service.WebPoolClient(config))
    assert [(call["method"], call["path"]) for call in calls] == [
        ("POST", "/api/accounts/update"), ("POST", "/api/accounts/sync"),
        ("GET", "/api/accounts/operations/op_1"), ("GET", "/api/accounts/operations/op_1"),
        ("GET", f"/api/accounts/{REMOTE_ID}"),
    ]
    assert result["ready"] is True
    state = sync_state(row.id)
    assert state["remote_id"] == REMOTE_ID
    assert state["credential_fingerprint"] == credential_fingerprint("new-web-at")
    assert state["needs_resync"] is False
    assert service.pending_count(config) == 0


def test_resync_still_runs_above_target_without_registering_or_creating(monkeypatch):
    config = settings(target_value=1)
    row = resync_account(config)
    calls = resync_http(monkeypatch)
    register = Mock()
    result = run(config, register)
    assert result.success == 1
    assert result.registrations == 0
    assert not result.errors
    register.assert_not_called()
    assert sync_state(row.id)["needs_resync"] is False
    assert all(call["path"] != "/api/accounts" for call in calls if call["method"] == "POST")


def test_uncertain_resync_reuses_saved_id_and_never_creates(monkeypatch):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config, uncertain=True)
    calls = resync_http(monkeypatch)
    result = service.upload_account(row.id, config, service.WebPoolClient(config))
    assert result["ready"] is True
    assert calls[0]["path"] == "/api/accounts/update"
    assert sync_state(row.id)["uncertain"] is False


def test_imported_v3_record_without_remote_id_cannot_fall_back_to_create(monkeypatch):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config, remote_id="")
    request = Mock(side_effect=AssertionError("no network allowed without stable ID"))
    monkeypatch.setattr(requests.Session, "request", request)
    with pytest.raises(service.PoolError):
        service.upload_account(row.id, config, service.WebPoolClient(config))
    request.assert_not_called()
    assert sync_state(row.id)["needs_resync"] is True


def test_false_done_ignores_success_like_status_and_keeps_polling(monkeypatch):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config)
    calls = resync_http(monkeypatch, progresses=[
        operation(done=False, status="completed"), operation(),
    ])
    assert service.upload_account(row.id, config, service.WebPoolClient(config))["ready"] is True
    assert sum(call["path"].startswith("/api/accounts/operations/") for call in calls) == 2


@pytest.mark.parametrize("done", [None, "true", "false", 1, 0])
def test_non_boolean_done_cannot_confirm_resync(monkeypatch, done):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config)
    resync_http(monkeypatch, progresses=[operation(done=done, status="completed", result=updated())])
    with pytest.raises(service.PoolError):
        service.upload_account(row.id, config, service.WebPoolClient(config))
    assert_pending_resync(row, config)


@pytest.mark.parametrize("progress", [
    pytest.param(operation(stage="failed"), id="failed-stage-with-done-true"),
    pytest.param(operation(done=False, stage="failed", error="sync failed"), id="failed-operation"),
    pytest.param(operation(error="sync failed"), id="completed-with-error"),
    pytest.param(operation(done=False), id="timeout"),
    pytest.param(operation(result=updated(updated_ids=["acct_other"])), id="wrong-result-id"),
    pytest.param(operation(result=updated(removed_ids=[REMOTE_ID], removed=1)), id="removed-result"),
    pytest.param(operation(result=None), id="missing-result"),
])
def test_failed_timeout_or_wrong_result_resync_preserves_pending_state(monkeypatch, progress):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config)
    calls = resync_http(monkeypatch, progresses=[progress])
    with pytest.raises(service.PoolError):
        service.upload_account(row.id, config, service.WebPoolClient(config))
    assert_pending_resync(row, config)
    assert all(call["path"] != "/api/accounts" for call in calls if call["method"] == "POST")


@pytest.mark.parametrize("changes", [
    {"updated_ids": ["acct_other"]}, {"removed_ids": [REMOTE_ID], "removed": 1},
    {"updated": True}, {"errors": [{"id": REMOTE_ID, "error": "update failed"}]},
])
def test_rejected_or_unconfirmed_update_cannot_reach_sync(monkeypatch, changes):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config)
    calls = resync_http(monkeypatch, update=updated(**changes))
    with pytest.raises(service.PoolError):
        service.upload_account(row.id, config, service.WebPoolClient(config))
    assert_pending_resync(row, config)
    assert not any(call["path"] == "/api/accounts/sync" for call in calls)


@pytest.mark.parametrize("changes", [
    {"progress_id": ""}, {"target_ids": ["acct_other"]}, {"target_ids": []},
    {"errors": [{"id": REMOTE_ID, "error": "not scheduled"}]},
])
def test_sync_ack_must_confirm_requested_id_before_polling(monkeypatch, changes):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config)
    sync = {"progress_id": "op_1", "target_ids": [REMOTE_ID], "errors": [], **changes}
    calls = resync_http(monkeypatch, sync=sync)
    with pytest.raises(service.PoolError):
        service.upload_account(row.id, config, service.WebPoolClient(config))
    assert_pending_resync(row, config)
    assert not any(call["path"].startswith("/api/accounts/operations/") for call in calls)


@pytest.mark.parametrize("detail", [
    {"item": v3_item("acct_other")}, {"items": [v3_item(REMOTE_ID)]}, {"item": None},
])
def test_detail_must_confirm_saved_id_even_when_email_matches(monkeypatch, detail):
    config = settings(protocol="yukkcat_v3")
    row = resync_account(config)
    resync_http(monkeypatch, detail=detail)
    with pytest.raises(service.PoolError):
        service.upload_account(row.id, config, service.WebPoolClient(config))
    assert_pending_resync(row, config)


def test_completed_resync_with_mutation_errors_counts_no_success(monkeypatch):
    config = settings(target_value=1)
    row = resync_account(config)
    resync_http(monkeypatch, progresses=[operation(result=updated(
        errors=[{"id": REMOTE_ID, "error": "refresh failed"}],
    ))])
    register = Mock()
    result = run(config, register)
    assert result.success == result.registrations == 0
    assert result.errors
    assert sync_state(row.id)["remote_id"] == REMOTE_ID
    assert sync_state(row.id)["ready"] is False
    register.assert_not_called()
    assert service.daily_used() == 0


def test_failed_resync_counts_no_success_and_spends_no_registration_budget(monkeypatch):
    config = settings(target_value=1)
    row = resync_account(config)
    resync_http(monkeypatch, progresses=[operation(done=False, stage="failed", error="offline")])
    register = Mock()
    result = run(config, register)
    assert result.success == result.registrations == 0
    assert result.errors
    assert result.retryable_failure is True
    assert_pending_resync(row, config)
    register.assert_not_called()
    assert service.daily_used() == 0


def test_legacy_auto_detection_upload_and_quota_semantics_remain_compatible(monkeypatch):
    config = settings()
    row = local_account(config, token="legacy-at")
    legacy = {"status": "正常", "quota": 5, "access_token": "legacy-at"}
    calls = scripted_http(monkeypatch, [
        ("GET", "/api/accounts", {"items": [legacy, {"status": "限流", "quota": 99}]}),
        ("POST", "/api/accounts", {"added": 1, "skipped": 0, "refreshed": 1, "errors": [], "items": [legacy]}),
    ])
    client = service.WebPoolClient(config)
    snapshot = client.snapshot()
    assert snapshot["protocol"] == "legacy"
    assert snapshot["remaining_quota"] == 5
    assert snapshot["available_accounts"] == 1
    result = service.upload_account(row.id, config, client)
    assert result["ready"] is True
    assert calls[-1]["payload"] == {"accounts": [{"access_token": "legacy-at", "email": row.email}]}
    assert service.pending_count(config) == 0


def test_legacy_uncertain_import_reconciles_exact_token_without_another_post(monkeypatch):
    config = settings(protocol="legacy")
    row = local_account(config, token="legacy-at", state={"imported": False, "uncertain": True})
    calls = scripted_http(monkeypatch, [("GET", "/api/accounts", {
        "items": [{"status": "正常", "quota": 5, "access_token": "legacy-at"}],
    })])
    result = service.upload_account(row.id, config, service.WebPoolClient(config))
    assert result["ready"] is True
    assert len(calls) == 1
    assert sync_state(row.id)["uncertain"] is False


def test_legacy_unknown_quota_still_fails_closed(monkeypatch):
    scripted_http(monkeypatch, [("GET", "/api/accounts", {
        "items": [{"status": "正常", "quota": 0, "image_quota_unknown": True}],
    })])
    register = Mock()
    with pytest.raises(service.PoolError):
        run(settings(), register)
    register.assert_not_called()
    assert service.daily_used() == 0
