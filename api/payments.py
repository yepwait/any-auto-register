"""统一支付渠道 API。"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from core.db import AccountModel, engine, get_session
from core.config_store import config_store
from core.resource_limits import account_slot
from services.payment_channels.contracts import PaymentAccount
from services.payment_channels.registry import load_builtin_payment_channels, payment_channels
from services.payment_channels.service import account_context, create_link_for_context, pay_for_context

router = APIRouter(prefix="/payments", tags=["payments"])
logger = logging.getLogger(__name__)


class PaymentRequest(BaseModel):
    channel: str = "direct"
    options: dict[str, Any] = Field(default_factory=dict)


class PaymentTaskRequest(PaymentRequest):
    account_ids: list[int] = Field(min_length=1, max_length=1000)
    operation: str = "link"
    concurrency: int = Field(default=1, ge=1, le=10)
    delay_seconds: float = Field(default=0, ge=0, le=3600)


class CardCreateRequest(BaseModel):
    number: str = Field(min_length=12, max_length=24)
    exp_month: str = Field(min_length=1, max_length=2)
    exp_year: str = Field(min_length=2, max_length=4)
    cvc: str = Field(min_length=3, max_length=4)
    name: str = ""
    brand: str = ""
    source: str = "manual"
    max_uses: int = Field(default=10, ge=1, le=100000)
    note: str = ""


def _account(account_id: int, session: Session) -> AccountModel:
    account = session.get(AccountModel, account_id)
    if not account or account.platform != "chatgpt":
        raise HTTPException(404, "账号不存在")
    return account


def _account_snapshot(account_id: int, session: Session | None = None) -> PaymentAccount:
    # Keep explicit-session Python callers working, but release their read
    # transaction before waiting for a worker slot or making network requests.
    if session is not None:
        try:
            return account_context(_account(account_id, session))
        finally:
            session.close()
    with Session(engine) as read_session:
        return account_context(_account(account_id, read_session))


def _configured_payment_proxy(operation: str, options: dict[str, Any]) -> str | None:
    """Use the operation-specific proxy for task-level logging and fallback."""
    key = "link_proxy" if operation == "link" else "pay_proxy"
    value = str(options.get(key) or "").strip()
    if value:
        return value
    config_key = "payment_link_proxy" if operation == "link" else "payment_pay_proxy"
    value = str(config_store.get(config_key, "") or "").strip()
    if value:
        return value
    # Keep tasks created by older clients working during the migration.
    value = str(config_store.get("payment_proxy", "") or "").strip()
    return value or None


def _payment_request_options(
    operation: str, options: dict[str, Any], proxy: str | None
) -> dict[str, Any]:
    request_options = dict(options)
    if proxy:
        operation_proxy_key = "link_proxy" if operation == "link" else "pay_proxy"
        request_options.setdefault(operation_proxy_key, proxy)
        request_options.setdefault("proxy", proxy)
    return request_options


def _direct_cards():
    from platforms.chatgpt.payment_channels.direct.card_store import card_store

    return card_store


def _require_channel(channel: str):
    load_builtin_payment_channels()
    try:
        return payment_channels.get(channel)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc


def _public_card(card: dict[str, Any]) -> dict[str, Any]:
    number = re.sub(r"\D", "", str(card.get("number") or ""))
    return {
        "id": int(card.get("id") or 0),
        "brand": str(card.get("brand") or "").upper(),
        "last4": number[-4:] if number else "",
        "name": str(card.get("name") or ""),
        "source": str(card.get("source") or ""),
        "uses": int(card.get("uses") or 0),
        "max_uses": int(card.get("max_uses") or 0),
        "note": str(card.get("note") or ""),
        "created_at": card.get("created_at"),
        "updated_at": card.get("updated_at"),
    }


@router.get("/channels")
def list_channels() -> dict[str, Any]:
    load_builtin_payment_channels()
    return {"channels": payment_channels.list()}


@router.get("/channels/{channel}/cards")
def list_cards(channel: str) -> dict[str, Any]:
    _require_channel(channel)
    if channel.lower() != "direct":
        raise HTTPException(404, "该渠道没有卡片资源")
    return {"cards": [_public_card(card) for card in _direct_cards().list_cards()]}


@router.post("/channels/{channel}/cards")
def add_card(channel: str, request: CardCreateRequest) -> dict[str, Any]:
    _require_channel(channel)
    if channel.lower() != "direct":
        raise HTTPException(404, "该渠道没有卡片资源")
    card_id = _direct_cards().add_card(request.model_dump())
    card = _direct_cards().get_card(card_id)
    return {"card": _public_card(card or {"id": card_id})}


@router.delete("/channels/{channel}/cards/{card_id}")
def delete_card(channel: str, card_id: int) -> dict[str, Any]:
    _require_channel(channel)
    if channel.lower() != "direct":
        raise HTTPException(404, "该渠道没有卡片资源")
    if not _direct_cards().delete_card(card_id):
        raise HTTPException(404, "卡片不存在")
    return {"ok": True, "card_id": card_id}


@router.post("/channels/{channel}/cards/reset-uses")
def reset_card_uses(channel: str) -> dict[str, Any]:
    _require_channel(channel)
    if channel.lower() != "direct":
        raise HTTPException(404, "该渠道没有卡片资源")
    return {"ok": True, "updated": _direct_cards().reset_uses()}


def _safe_result(result: Any, email: str, account_id: int) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "account_id": account_id,
        "email": email,
        "ok": bool(result.ok),
        "channel": result.channel,
        "operation": result.operation,
        "link": str(data.get("link") or ""),
        "checkout_session_id": str(data.get("checkout_session_id") or ""),
        "billing_country": str(data.get("billing_country") or ""),
        "subscription_plan": str(data.get("subscription_plan") or ""),
        "card_last4": str(data.get("card_last4") or ""),
        "error": str(result.error or ""),
    }


def _fail_payment_task(task_id: str, error: str) -> None:
    """Release admission before best-effort persistence, retaining task history."""
    from api import tasks

    snapshot = tasks._task_store.snapshot(task_id)
    if snapshot["status"] in {"pending", "running"}:
        tasks._task_store.finish(
            task_id,
            status="failed",
            success=snapshot["success"],
            registered=snapshot["registered"],
            skipped=snapshot["skipped"],
            errors=snapshot["errors"],
            error=error,
        )
    try:
        tasks._log(task_id, f"[FAIL] {error}")
    except Exception:
        logger.exception("Could not log payment task failure: %s", task_id)
    try:
        tasks._persist_task_snapshot(task_id)
    except Exception:
        # The normal writer can retry a temporary database failure. Do not
        # clean up the in-memory record until its terminal state is durable.
        with tasks._log_io_lock:
            tasks._dirty_task_ids.add(task_id)
        logger.exception("Could not persist payment task failure: %s", task_id)
    else:
        tasks._task_store.cleanup()


def _run_payment_task(
    task_id: str,
    account_ids: list[int],
    operation: str,
    channel: str,
    options: dict[str, Any],
    concurrency: int,
    delay_seconds: float,
    proxy: str | None = None,
) -> None:
    from api.tasks import _log, _persist_task_snapshot, _run_account_batch_task, _task_store
    from core.task_runtime import AttemptResult

    def handle_account(*, account_id, fields, proxy, control, attempt_id) -> AttemptResult:
        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
            if account is None or account.platform != "chatgpt":
                return AttemptResult.skipped("账号不存在")
            context = account_context(account)
        request_options = _payment_request_options(operation, options, proxy)
        result = create_link_for_context(context, channel, options=request_options) if operation == "link" else pay_for_context(context, channel, options=request_options)
        safe = _safe_result(result, fields["email"], account_id)
        _task_store.append_meta_list(task_id, "results", safe)
        _persist_task_snapshot(task_id)
        if result.ok:
            _log(task_id, f"[OK] {fields['email']} {operation} 完成")
            if safe.get("link"):
                _log(task_id, f"  链接: {safe['link']}")
            return AttemptResult.success()
        _log(task_id, f"[FAIL] {fields['email']} {result.error or '支付失败'}")
        return AttemptResult.failed(result.error or "支付失败")

    try:
        _run_account_batch_task(
            task_id,
            account_ids,
            label=f"{channel} {'提链' if operation == 'link' else '支付'}",
            concurrency=concurrency,
            delay_seconds=delay_seconds,
            proxy=proxy,
            handle_account=handle_account,
        )
    except Exception as exc:
        # Scheduler setup/final persistence can fail outside its own worker
        # exception handler; never leave an accepted payment pending forever.
        _fail_payment_task(task_id, f"支付任务执行异常: {exc}")


@router.post("/jobs")
def create_payment_task(request: PaymentTaskRequest, background_tasks: BackgroundTasks, session: Session = Depends(get_session)):
    operation = request.operation.strip().lower()
    if operation not in {"link", "pay"}:
        raise HTTPException(400, "operation 必须是 link 或 pay")
    _require_channel(request.channel)
    account_ids = list(dict.fromkeys(int(account_id) for account_id in request.account_ids))
    try:
        for account_id in account_ids:
            _account(account_id, session)
    finally:
        # Yield dependencies may otherwise retain their connection through the
        # response's background work on some FastAPI versions.
        session.close()
    task_proxy = _configured_payment_proxy(operation, request.options)
    task_id = f"payment_{operation}_{uuid4().hex}"
    from api import tasks

    with tasks.chatgpt_enqueue_lock:
        tasks.ensure_task_capacity()
        tasks._task_store.create(
            task_id,
            platform="chatgpt",
            total=len(account_ids),
            source="payment",
            meta={
                "kind": "payment",
                "operation": operation,
                "channel": request.channel,
                "account_ids": account_ids,
                "options": {k: v for k, v in request.options.items() if k not in {"card", "number", "cvc"}},
                "results": [],
            },
        )
        try:
            tasks._persist_task_snapshot(task_id)
            tasks._log(task_id, f"待处理账号 {len(account_ids)} 个")
            background_tasks.add_task(
                _run_payment_task,
                task_id,
                account_ids,
                operation,
                request.channel,
                dict(request.options),
                request.concurrency,
                request.delay_seconds,
                proxy=task_proxy,
            )
        except Exception as exc:
            _fail_payment_task(task_id, f"支付任务调度失败: {exc}")
            raise HTTPException(
                500,
                {"message": "支付任务调度失败", "task_id": task_id},
            ) from exc
    return {"task_id": task_id, "total": len(account_ids)}


def create_payment_link(account_id: int, request: PaymentRequest, session: Session | None = None):
    context = _account_snapshot(account_id, session)
    with account_slot():
        result = create_link_for_context(context, request.channel, options=request.options)
    return result.as_dict()


@router.post("/{account_id}/link", name="create_payment_link")
def _create_payment_link_endpoint(account_id: int, request: PaymentRequest):
    return create_payment_link(account_id, request)


def execute_payment(account_id: int, request: PaymentRequest, session: Session | None = None):
    context = _account_snapshot(account_id, session)
    with account_slot():
        result = pay_for_context(context, request.channel, options=request.options)
    return result.as_dict()


@router.post("/{account_id}/pay", name="execute_payment")
def _execute_payment_endpoint(account_id: int, request: PaymentRequest):
    return execute_payment(account_id, request)
