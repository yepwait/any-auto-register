"""账号生命周期修复后的离线验证脚本（无网络、无真实账号）。

修复前的 9 个缺陷曾由同一路径复现；本文件现在锁定修复后的新行为，作为快速自检。
正式回归见 tests/test_account_lifecycle_fixes.py。

运行：
    python docs/reviews/account_lifecycle_repro.py
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["DATABASE_URL"] = "sqlite://"
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def no_network(*args, **kwargs):
    raise AssertionError("External operations are forbidden in this offline verification")


socket.socket.connect = no_network
socket.socket.connect_ex = no_network
import requests
import curl_cffi.requests
requests.sessions.Session.request = no_network
curl_cffi.requests.Session.request = no_network
logging.disable(logging.CRITICAL)

from sqlmodel import Session, SQLModel
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

SQLModel.metadata.create_all(engine)
checks = []


def check(name, condition):
    checks.append({"check": name, "ok": bool(condition)})
    assert condition, name


def make(extra=None, status="registered", token="at", email="verify@example.invalid"):
    row = AccountModel(platform="chatgpt", email=email, password="pw", token=token, status=status)
    row.set_extra(extra or {})
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def load(row_id):
    with Session(engine) as session:
        return session.get(AccountModel, row_id)


check("rt_ready_without_sms", account_rt_ready(make({"refresh_token": "rt"}, email="a@x")))
check("sms_only_not_ready", not account_rt_ready(make({"sms_verification": {"verified": True}}, token="", email="b@x")))
check("ready_not_pending", not account_rt_recovery_pending(make({"refresh_token": "rt"}, email="c@x")))

row = make({"refresh_token": "old"}, status="subscribed", email="d@x")
apply_backfill_result(row, BackfillResult(success=True, refresh_token="new"))
check("subscription_preserved", row.status == "subscribed")

row = make({}, email="e@x")
with patch("services.chatgpt_rt_backfill.backfill_account_data", Mock(return_value=BackfillResult(success=True, refresh_token="rt-new"))), \
     patch.object(sms_stock_service, "_probe_after_backfill", lambda *a, **k: ""):
    ok = sms_stock_service.supplement_account(
        row, sms_stock_service.SmsStockSettings(), proxy=None,
        control=RegisterTaskControl(), attempt_id=None, log=lambda _m: None,
    )
check("supplement_success_on_rt_without_sms", ok and load(row.id).get_extra()["refresh_token"] == "rt-new")

row = make({"refresh_token": "old-rt"}, email="stop@x")
control = RegisterTaskControl()


def backfill_then_stop(**kwargs):
    control.request_stop()
    return BackfillResult(success=True, refresh_token="new-rt", access_token="new-at")


with patch("services.chatgpt_rt_backfill.backfill_account_data", side_effect=backfill_then_stop):
    try:
        inspection_service._backfill_and_persist(row.id, row, inspection_service.InspectionSettings(), None, lambda _m: None, control, None)
    except StopTaskRequested:
        pass
check("stop_still_persists_rt", load(row.id).get_extra()["refresh_token"] == "new-rt")

row = make({"refresh_token": "old-rt"}, email="probe@x")


def fake_probe(source, proxy=None):
    source.access_token = "rotated-at"
    source.extra["access_token"] = "rotated-at"
    source.extra["refresh_token"] = "rotated-rt"
    raise TimeoutError("offline")


with patch("platforms.chatgpt.status_probe.probe_local_chatgpt_status", side_effect=fake_probe):
    inspection_service.inspect_account(
        account_id=row.id, fields={"email": row.email, "token": row.token, "extra": row.get_extra()},
        settings=inspection_service.InspectionSettings(), proxy=None, log_fn=lambda _m: None,
    )
check("probe_exception_persists_rt", load(row.id).get_extra()["refresh_token"] == "rotated-rt")

ctrl = SimpleNamespace(provider_key="smsbower", config={"sms_max_phone_attempts": "3"},
                       get_phone=Mock(side_effect=SmsProviderFatalError("NO_BALANCE")))
flow = AuthFlow.__new__(AuthFlow)
flow._get_env = lambda key, default="": default
try:
    flow._do_sms_loop(ctrl)
except SmsProviderFatalError:
    pass
check("no_balance_single_attempt", ctrl.get_phone.call_count == 1)

engine.dispose()
print(json.dumps({"checks": len(checks), "passed": sum(c["ok"] for c in checks), "network": "blocked", "results": checks}, ensure_ascii=False, indent=2))
