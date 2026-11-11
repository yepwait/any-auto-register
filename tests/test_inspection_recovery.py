"""巡检恢复：只模拟网络与补 RT，验证真实的落库和最终上传门禁。"""
from unittest.mock import Mock

import pytest
from sqlmodel import Session

from core.db import AccountModel, engine
from core.task_runtime import AttemptOutcome, StopTaskRequested
from platforms.chatgpt.rt_backfill import BackfillResult
from platforms.chatgpt import status_probe
from services import inspection_service as service


def probe(auth="access_token_valid", codex="usable"):
    return {
        "auth": {"state": auth, "http_status": 200 if auth == "access_token_valid" else 401},
        "codex": {"state": codex},
    }


@pytest.fixture
def inspection(monkeypatch):
    with Session(engine) as session:
        row = AccountModel(platform="chatgpt", email="recover@example.invalid", password="pw", token="old")
        row.set_extra({"refresh_token": "old-rt"})
        session.add(row)
        session.commit()
        session.refresh(row)
        account_id = row.id
    upload = Mock(return_value=(True, "ok"))
    monkeypatch.setattr(service, "_upload_chat2api", upload)
    monkeypatch.setattr(service.config_store, "get_all", lambda: {})
    backfill = Mock(return_value=BackfillResult(success=True, refresh_token="new-rt", access_token="new-at", strategy="login"))
    monkeypatch.setattr("services.chatgpt_rt_backfill.backfill_account_data", backfill)

    def run(**settings):
        return service.inspect_account(
            account_id=account_id,
            fields={"email": "recover@example.invalid", "password": "pw", "token": "old", "extra": {"refresh_token": "old-rt"}},
            settings=service.InspectionSettings(chat2api_enabled=True, **settings),
            proxy=None, log_fn=lambda _message: None,
        )
    return run, upload, backfill


def test_unauthorized_retries_then_backfills_even_when_old_rt_exists(monkeypatch, inspection):
    run, upload, backfill = inspection
    measure = Mock(side_effect=[probe("unauthorized", "skipped_auth_invalid"), probe("unauthorized", "skipped_auth_invalid"), probe()])
    monkeypatch.setattr(status_probe, "probe_local_chatgpt_status", measure)
    outcome = run(backfill_rt_enabled=True, backfill_allow_login=True, only_missing_rt=True)
    assert outcome.outcome == AttemptOutcome.SUCCESS
    assert measure.call_count == 3
    backfill.assert_called_once()
    assert backfill.call_args.kwargs["allow_login"] is True
    assert measure.call_args.args[0].access_token == "new-at"
    assert upload.call_args.args[0].access_token == "new-at"
    assert upload.call_args.args[1]["auth"]["state"] == "access_token_valid"


def test_repeat_probe_can_recover_without_login(monkeypatch, inspection):
    run, upload, backfill = inspection
    monkeypatch.setattr(status_probe, "probe_local_chatgpt_status", Mock(side_effect=[probe("unauthorized", "skipped_auth_invalid"), probe()]))
    assert run(backfill_rt_enabled=False).outcome == AttemptOutcome.SUCCESS
    backfill.assert_not_called()
    upload.assert_called_once()


def test_recovery_must_pass_fresh_probe_before_upload(monkeypatch, inspection):
    run, upload, backfill = inspection
    monkeypatch.setattr(status_probe, "probe_local_chatgpt_status", Mock(return_value=probe("unauthorized", "skipped_auth_invalid")))
    assert run(backfill_rt_enabled=True).outcome == AttemptOutcome.FAILED
    backfill.assert_called_once()
    upload.assert_not_called()


def test_disabled_recovery_does_not_trigger_backfill(monkeypatch, inspection):
    run, upload, backfill = inspection
    monkeypatch.setattr(status_probe, "probe_local_chatgpt_status", Mock(return_value=probe("unauthorized", "skipped_auth_invalid")))
    assert run(backfill_rt_enabled=False).outcome == AttemptOutcome.FAILED
    backfill.assert_not_called()
    upload.assert_not_called()


def test_explicit_deactivation_never_retries_or_logs_in(monkeypatch, inspection):
    run, upload, backfill = inspection
    measure = Mock(return_value=probe("account_deactivated", "skipped_auth_invalid"))
    monkeypatch.setattr(status_probe, "probe_local_chatgpt_status", measure)
    assert run(backfill_rt_enabled=True, backfill_allow_login=True).outcome == AttemptOutcome.FAILED
    assert measure.call_count == 1
    backfill.assert_not_called()
    upload.assert_not_called()


def test_probe_exception_does_not_trigger_recovery_or_upload(monkeypatch, inspection):
    run, upload, backfill = inspection
    monkeypatch.setattr(status_probe, "probe_local_chatgpt_status", Mock(side_effect=TimeoutError))
    assert run(backfill_rt_enabled=True).outcome == AttemptOutcome.FAILED
    backfill.assert_not_called()
    upload.assert_not_called()


def test_stop_is_not_swallowed(monkeypatch, inspection):
    run, upload, _ = inspection
    monkeypatch.setattr(status_probe, "probe_local_chatgpt_status", Mock(side_effect=StopTaskRequested))
    with pytest.raises(StopTaskRequested):
        run(backfill_rt_enabled=True)
    upload.assert_not_called()
