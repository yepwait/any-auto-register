"""Credential rotation and threshold regressions; no external network calls."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from core.db import AccountModel
from core.task_runtime import RegisterTaskControl
from services import chatgpt_web_pool as pool
from services.chatgpt_account_purpose import WEB_POOL_PURPOSE, credential_fingerprint
from services.chatgpt_rt_backfill import apply_backfill_result
from services.chatgpt_sync import _merge_account_credentials
from platforms.chatgpt.rt_backfill import BackfillResult


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(pool, "engine", engine)
    saved = {}
    monkeypatch.setattr(pool.config_store, "get", lambda key, default="": saved.get(key, default))
    monkeypatch.setattr(pool.config_store, "set_many", saved.update)
    yield saved
    engine.dispose()


def settings(**kwargs):
    return pool.WebPoolSettings(base_url="https://pool.invalid", admin_token="test", delay_seconds=0, **kwargs)


def account(*, web=True, imported=True):
    row = AccountModel(platform="chatgpt", email="test@example.invalid", password="test", token="old-at")
    extra = {"access_token": "old-at"}
    if web:
        extra.update(account_purpose=WEB_POOL_PURPOSE, chatgpt_web_pool_target=settings().target_id)
    if imported:
        extra["sync_statuses"] = {"chatgpt2api": {"imported": True, "ready": True, "remote_id": "acct_saved",
             "credential_fingerprint": credential_fingerprint("old-at")}}
    row.set_extra(extra)
    with Session(pool.engine) as session:
        session.add(row); session.commit(); session.refresh(row)
        return row


def snapshot(quota):
    return pool.summarize([{"status": "正常", "quota": quota}])


def test_rt_backfill_marks_rotated_web_credentials_for_resync():
    row = account()
    apply_backfill_result(row, BackfillResult(success=True, access_token="new-at", refresh_token="new-rt"))
    state = row.get_extra()["sync_statuses"]["chatgpt2api"]
    assert state["needs_resync"] is True and state["ready"] is False
    assert state["remote_id"] == "acct_saved"
    assert state["credential_fingerprint"] == credential_fingerprint("old-at")
    assert "new-at" not in str(state) and "new-rt" not in str(state)


def test_probe_merge_also_marks_resync_but_unchanged_and_nonweb_do_not():
    row = account()
    _merge_account_credentials(row, SimpleNamespace(extra={"access_token": "new-at"}))
    assert row.get_extra()["sync_statuses"]["chatgpt2api"]["needs_resync"]
    for candidate in (account(web=False), account()):
        _merge_account_credentials(candidate, SimpleNamespace(extra={"access_token": "old-at"}))
        assert not candidate.get_extra()["sync_statuses"]["chatgpt2api"].get("needs_resync")


def test_changed_credential_enters_pending_without_clearing_remote_id():
    row = account()
    with Session(pool.engine) as session:
        stored = session.get(AccountModel, row.id)
        apply_backfill_result(stored, BackfillResult(success=True, access_token="new-at"), session=session, commit=True)
    assert [item.id for item in pool._pending_accounts(settings())] == [row.id]


def test_resync_runs_when_pool_already_sufficient_without_new_registration(monkeypatch):
    row = account()
    with Session(pool.engine) as session:
        stored = session.get(AccountModel, row.id)
        apply_backfill_result(stored, BackfillResult(success=True, access_token="new-at"), session=session, commit=True)
    monkeypatch.setattr(pool.WebPoolClient, "snapshot", Mock(return_value=snapshot(200)))
    uploaded = Mock(return_value={"imported": True, "ready": True, "quota": 5, "status": "正常", "remote_id": "acct_saved"})
    monkeypatch.setattr(pool.WebPoolClient, "upload", uploaded)
    register = Mock(side_effect=AssertionError("do not register when sufficient"))
    result = pool.replenish(settings(), control=RegisterTaskControl(), log=lambda _: None,
                            progress=lambda _: None, register=register, should_continue=lambda: True)
    assert result.success == 1 and result.registrations == 0
    register.assert_not_called()
    assert pool.pending_count(settings()) == 0
    with Session(pool.engine) as session:
        state = session.get(AccountModel, row.id).get_extra()["sync_statuses"]["chatgpt2api"]
    assert not state["needs_resync"]
    assert state["credential_fingerprint"] == credential_fingerprint("new-at")


def test_credential_changes_during_upload_cannot_be_falsely_marked_synced(monkeypatch):
    row = account()
    client = Mock()
    def upload(_):
        with Session(pool.engine) as session:
            latest = session.get(AccountModel, row.id)
            extra = latest.get_extra(); extra["access_token"] = "newer-at"
            latest.token = "newer-at"; latest.set_extra(extra); session.add(latest); session.commit()
        return {"imported": True, "ready": True, "quota": 5, "status": "正常"}
    client.upload.side_effect = upload
    pool.upload_account(row.id, settings(), client)
    with Session(pool.engine) as session:
        state = session.get(AccountModel, row.id).get_extra()["sync_statuses"]["chatgpt2api"]
    assert state["needs_resync"] and not state["ready"]
    assert state["credential_fingerprint"] == credential_fingerprint("old-at")


def test_failed_resync_keeps_old_identity_and_is_retryable():
    row = account()
    client = Mock()
    client.upload.side_effect = pool.PoolError("sync failed")
    with pytest.raises(pool.PoolError): pool.upload_account(row.id, settings(), client)
    with Session(pool.engine) as session:
        state = session.get(AccountModel, row.id).get_extra()["sync_statuses"]["chatgpt2api"]
    assert state["needs_resync"] and state["remote_id"] == "acct_saved"
    assert state["imported"]
    assert pool.pending_count(settings()) == 1


def test_threshold_validation_and_legacy_default():
    assert settings(target_value=2000).start_threshold == 2000
    assert settings(target_value=2300, refill_below=2000).start_threshold == 2000
    with pytest.raises(ValueError): settings(target_value=2000, refill_below=2300)


def test_hysteresis_does_not_start_in_middle_but_continues_latched(monkeypatch):
    cfg = settings(target_value=2300, refill_below=2000, max_attempts_per_run=1)
    monkeypatch.setattr(pool.WebPoolClient, "snapshot", Mock(return_value=snapshot(2100)))
    register = Mock(return_value=None)
    run = lambda active: pool.replenish(cfg, control=RegisterTaskControl(), log=lambda _: None,
        progress=lambda _: None, register=register, should_continue=lambda: True, refill_active=active)
    assert run(False).registrations == 0
    register.assert_not_called()
    assert run(True).registrations == 1


def test_target_stops_latched_refill(monkeypatch):
    cfg = settings(target_value=2300, refill_below=2000)
    monkeypatch.setattr(pool.WebPoolClient, "snapshot", Mock(return_value=snapshot(2300)))
    register = Mock(side_effect=AssertionError("at target"))
    result = pool.replenish(cfg, control=RegisterTaskControl(), log=lambda _: None, progress=lambda _: None,
        register=register, should_continue=lambda: True, refill_active=True)
    assert result.registrations == 0
    register.assert_not_called()
