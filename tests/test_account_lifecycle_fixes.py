"""账号生命周期修复的回归测试。

覆盖新口径：没有 add-phone、直接拿到有效 RT 且账态正常即算成功；短信验证只是
独立事实。同时锁定凭证保存、状态保留、停止控制与接码致命错误的边界。

全部使用内存/测试数据库与 mock，不访问网络、不租号、不注册真实账号。
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlmodel import Session

from core.db import AccountModel, engine
from core.task_runtime import RegisterTaskControl, StopTaskRequested
from platforms.chatgpt.protocol.auth_flow import AuthFlow
from platforms.chatgpt.rt_backfill import BackfillResult, RefreshTokenBackfiller
from platforms.chatgpt.status_probe import ProbeHTTPResult, probe_local_chatgpt_status
from platforms.chatgpt.token_refresh import TokenRefreshResult
from services import inspection_service, sms_stock_service
from services.chatgpt_account_state import account_rt_ready, account_rt_recovery_pending
from services.chatgpt_rt_backfill import apply_backfill_result
from services.sms_service import SmsProviderFatalError


def _row(extra=None, *, status="registered", token="at", password="pw", email="lifecycle@example.invalid"):
    row = AccountModel(platform="chatgpt", email=email, password=password, token=token, status=status)
    row.set_extra(extra or {})
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def _load(row_id):
    with Session(engine) as session:
        return session.get(AccountModel, row_id)


def test_rt_ready_without_sms_is_ready_and_sms_only_is_not():
    rt_only = _row({"refresh_token": "rt"}, email="rt-only@example.invalid")
    assert account_rt_ready(rt_only)
    assert sms_stock_service._in_stock(rt_only)

    sms_only = _row({"sms_verification": {"verified": True}}, token="", email="sms-only@example.invalid")
    assert not account_rt_ready(sms_only)
    assert not sms_stock_service._in_stock(sms_only)


def test_recovery_pending_excludes_ready_and_deleted():
    ready = _row({"refresh_token": "rt"}, email="ready@example.invalid")
    pending = _row({}, email="pending@example.invalid")
    dead = _row({"refresh_token": "rt"}, status="deleted", email="dead@example.invalid")
    assert not account_rt_recovery_pending(ready)
    assert account_rt_recovery_pending(pending)
    assert not account_rt_recovery_pending(dead)


def test_supplement_succeeds_on_rt_without_sms(monkeypatch):
    row = _row({}, email="supplement@example.invalid")
    monkeypatch.setattr(
        "services.chatgpt_rt_backfill.backfill_account_data",
        Mock(return_value=BackfillResult(success=True, refresh_token="rt-new")),
    )
    monkeypatch.setattr(sms_stock_service, "_probe_after_backfill", lambda *a, **k: "")
    ok = sms_stock_service.supplement_account(
        row, sms_stock_service.SmsStockSettings(), proxy=None,
        control=RegisterTaskControl(), attempt_id=None, log=lambda _m: None,
    )
    assert ok is True
    saved = _load(row.id)
    assert saved.get_extra()["refresh_token"] == "rt-new"
    assert not (saved.get_extra().get("sms_verification") or {}).get("verified")


@pytest.mark.parametrize("status", ["trial", "subscribed"])
def test_backfill_keeps_subscription_status(status):
    model = AccountModel(platform="chatgpt", email="sub@example.invalid", password="pw", token="at", status=status)
    model.set_extra({})
    apply_backfill_result(model, BackfillResult(success=True, refresh_token="rt"))
    assert model.status == status


def test_backfill_clears_invalid_to_registered():
    model = AccountModel(platform="chatgpt", email="inv@example.invalid", password="pw", token="at", status="invalid")
    model.set_extra({})
    apply_backfill_result(model, BackfillResult(success=True, refresh_token="rt"))
    assert model.status == "registered"


def _jwt_with_account_id(account_id):
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"hdr.{body}.sig"


class _DummyAccount:
    def __init__(self, **kw):
        self.email = "probe@example.invalid"
        self.token = kw.get("token", "")
        self.access_token = kw.get("access_token", "")
        self.refresh_token = kw.get("refresh_token", "")
        self.session_token = kw.get("session_token", "")
        self.user_id = kw.get("user_id", "")
        self.extra = dict(kw.get("extra") or {})


def test_probe_reparses_account_id_after_refresh():
    account = _DummyAccount(token="old-at", refresh_token="rt", user_id="")
    rejected = ProbeHTTPResult(401, {}, "", {}, "", "unauthorized")
    valid = ProbeHTTPResult(200, {}, "{}", {"plan_type": "free"}, "", "ok")
    new_at = _jwt_with_account_id("acct-new")
    with patch("platforms.chatgpt.status_probe._probe_backend_me", side_effect=[rejected, valid]), \
         patch("platforms.chatgpt.status_probe._probe_codex_usage", return_value=valid) as codex, \
         patch("platforms.chatgpt.token_refresh.TokenRefreshManager.refresh_by_oauth_token",
               return_value=TokenRefreshResult(success=True, access_token=new_at)):
        result = probe_local_chatgpt_status(account)
    assert result["auth"]["state"] == "access_token_valid"
    assert result["codex"]["state"] == "usable"
    assert codex.call_count == 1


def test_stop_after_backfill_still_persists_new_rt():
    row = _row({"refresh_token": "old-rt"}, email="stop@example.invalid")
    control = RegisterTaskControl()

    def backfill_then_stop(**kwargs):
        control.request_stop()
        return BackfillResult(success=True, refresh_token="new-rt", access_token="new-at")

    with patch("services.chatgpt_rt_backfill.backfill_account_data", side_effect=backfill_then_stop):
        with pytest.raises(StopTaskRequested):
            inspection_service._backfill_and_persist(
                row.id, row, inspection_service.InspectionSettings(), None, lambda _m: None, control, None
            )
    assert _load(row.id).get_extra()["refresh_token"] == "new-rt"


def test_probe_exception_still_persists_rotated_credentials():
    row = _row({"refresh_token": "old-rt"}, email="probe-exc@example.invalid")

    def fake_probe(source, proxy=None):
        source.access_token = "rotated-at"
        source.extra["access_token"] = "rotated-at"
        source.extra["refresh_token"] = "rotated-rt"
        raise TimeoutError("offline timeout")

    with patch("platforms.chatgpt.status_probe.probe_local_chatgpt_status", side_effect=fake_probe):
        outcome = inspection_service.inspect_account(
            account_id=row.id,
            fields={"email": row.email, "token": row.token, "extra": row.get_extra()},
            settings=inspection_service.InspectionSettings(),
            proxy=None,
            log_fn=lambda _m: None,
        )
    assert outcome.outcome.value == "failed"
    assert _load(row.id).get_extra()["refresh_token"] == "rotated-rt"


class _DeactivatingFlow:
    def __init__(self):
        self.result = SimpleNamespace(
            refresh_token="", access_token="", session_token="", id_token="",
            cookie_header="", sms_verification={},
        )

    def from_existing_credentials(self, session_token, access_token, device_id):
        self.result.session_token = session_token
        self.result.access_token = access_token or "at"
        return self.result

    def oauth_codex_rt_exchange(self, mail_provider=None):
        self.result.refresh_token = "new-rt"
        raise RuntimeError("403 - You do not have an account because it has been deleted or deactivated.")


def test_deactivation_signal_beats_rt_success():
    backfiller = RefreshTokenBackfiller(email="deact@example.invalid", session_token="s", extra_config={})
    flow = _DeactivatingFlow()
    backfiller._build_flow = lambda _o: flow
    result = backfiller.run()
    assert result.success is True
    assert result.account_deleted is True


def test_no_balance_stops_after_first_attempt():
    ctrl = SimpleNamespace(
        provider_key="smsbower",
        config={"sms_max_phone_attempts": "3"},
        get_phone=Mock(side_effect=SmsProviderFatalError("NO_BALANCE")),
    )
    flow = AuthFlow.__new__(AuthFlow)
    flow._get_env = lambda key, default="": default
    with pytest.raises(SmsProviderFatalError):
        flow._do_sms_loop(ctrl)
    assert ctrl.get_phone.call_count == 1


def test_proxy_url_is_masked_in_logs():
    from api.tasks import _mask_proxy_url

    masked = _mask_proxy_url("http://user:p@ssw0rd@host.example:8080/path")
    assert "p@ssw0rd" not in masked and "user" not in masked
    assert "host.example:8080" in masked
