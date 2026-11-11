"""接码账号库存策略与有界补库流程。

统计、候选选择、费用尝试限额在此；API 负责创建/控制后台任务，协议层负责接码。
数据库连接不跨网络操作持有；只有真实的短信验证记录能计入库存。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import time
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, engine
from core.task_runtime import SkipCurrentAttemptRequested, StopTaskRequested
from services.account_locks import get_account_lock
from services.chatgpt_account_state import (
    account_rt_ready,
    account_rt_recovery_pending,
    classify_local_probe_state,
)

SOURCE = "sms_stock"
REGISTER_SOURCE = "sms_stock_register"
NORMAL_STATUSES = {"registered", "trial", "subscribed"}
_budget_lock = threading.Lock()


class SmsStockSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    min_accounts: int = Field(default=10, ge=1, le=5000)
    interval_minutes: int = Field(default=30, ge=1, le=10080)
    max_attempts_per_run: int = Field(default=5, ge=1, le=100)
    daily_attempt_limit: int = Field(default=20, ge=1, le=1000)
    max_consecutive_failures: int = Field(default=3, ge=1, le=10)
    delay_seconds: float = Field(default=5, ge=0, le=3600, allow_inf_nan=False)
    candidate_cooldown_minutes: int = Field(default=60, ge=1, le=10080)
    allow_login: bool = True
    register_flow: Literal["email", "phone", "phone_with_email"] = "phone_with_email"

    @classmethod
    def from_store(cls):
        return cls.model_validate({
            key: config_store.get(f"sms_stock_{key}", str(item.default))
            for key, item in cls.model_fields.items()
        })


def save_settings(settings: SmsStockSettings) -> None:
    config_store.set_many({
        f"sms_stock_{key}": ("1" if value else "0") if isinstance(value, bool) else str(value)
        for key, value in settings.model_dump().items()
    })


def validate_sms_config() -> None:
    """仅本地配置校验，不查余额、不租号、不消耗付费额度。"""
    from services.sms_service import create_sms_provider, resolve_sms_settings

    sms = resolve_sms_settings()
    if str(sms.get("sms_enabled", "")).strip().lower() not in {"1", "true", "yes", "on"}:
        raise ValueError("请先在全局配置启用接码并配置接码服务")
    try:
        create_sms_provider(str(sms.get("sms_provider") or "smsbower"), sms)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def _today() -> str:
    # 每日上限明确按 UTC 计算，服务重启和手动执行均不重置。
    return datetime.now(timezone.utc).date().isoformat()


def daily_attempts_used() -> int:
    if config_store.get("sms_stock_budget_date", "") != _today():
        return 0
    return max(0, int(config_store.get("sms_stock_budget_used", "0")))


def reserve_attempt(settings: SmsStockSettings) -> bool:
    with _budget_lock:
        used = daily_attempts_used()
        if used >= settings.daily_attempt_limit:
            return False
        # 先记账再执行；中断/崩溃也不能靠重启绕过当日尝试上限。
        config_store.set_many({"sms_stock_budget_date": _today(), "sms_stock_budget_used": str(used + 1)})
        return True


def _rows() -> list[AccountModel]:
    with Session(engine) as session:
        return list(session.exec(
            select(AccountModel).where(AccountModel.platform == "chatgpt").order_by(AccountModel.id)
        ).all())


def _eligible_status(account: AccountModel) -> bool:
    if account.status in {"deleted", "banned", "expired"}:
        return False
    reason = classify_local_probe_state(account.get_extra().get("chatgpt_local"))
    if reason in {"auth_deactivated", "codex_deactivated", "auth_403", "codex_403"}:
        return False
    return account.status in NORMAL_STATUSES or (
        account.status == "invalid" and reason in {"auth_401", "codex_401"}
    )


def _in_stock(account: AccountModel) -> bool:
    """可用 RT 账号：正常状态 + 有 refresh_token + 无明确不可用证据。

    短信验证不再作为库存条件；它只是账号 extra 里的一条独立事实。
    """
    return _eligible_status(account) and account_rt_ready(account)


def _pending(account: AccountModel) -> bool:
    """需要补 RT 的候选：尚未就绪，但仍有可尝试的凭证或密码。"""
    return _eligible_status(account) and account_rt_recovery_pending(account)


def _cooling(account: AccountModel, settings: SmsStockSettings, now: float) -> bool:
    record = account.get_extra().get("sms_stock_attempt") or {}
    last_at = float(record.get("at") or 0)
    return last_at > 0 and now - last_at < settings.candidate_cooldown_minutes * 60


def inventory(settings: SmsStockSettings) -> dict:
    rows = _rows()
    pending = [row for row in rows if _pending(row)]
    count = sum(_in_stock(row) for row in rows)
    used = daily_attempts_used()
    return {
        "verified_accounts": count,
        "pending_accounts": len(pending),
        "ready_candidates": sum(not _cooling(row, settings, time.time()) for row in pending),
        "missing_accounts": max(0, settings.min_accounts - count),
        "daily_attempts_used": used,
        "daily_attempts_remaining": max(0, settings.daily_attempt_limit - used),
        "budget_date_utc": _today(),
    }


def _next_candidate(settings: SmsStockSettings, attempted: set[int]) -> AccountModel | None:
    now = time.time()
    return next((row for row in _rows() if row.id not in attempted and _pending(row) and not _cooling(row, settings, now)), None)


def _mark_attempt(account_id: int, *, ok: bool = False, message: str = "进行中") -> None:
    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is None:
            raise ValueError("账号已被删除")
        extra = row.get_extra()
        extra["sms_stock_attempt"] = {"at": time.time(), "ok": ok, "message": message[:200]}
        row.set_extra(extra)
        session.add(row)
        session.commit()


def _probe_after_backfill(account_id: int, proxy, log) -> str:
    """补 RT 成功后做一次测活，返回空串表示没有异常。

    测活只是尽力而为：真正的成功标准是“拿到 RT 且账态正常”。测活明确判定失效会
    通过 account_rt_ready 反映；测活超时则保留新凭证，下次再验证，不丢成果。
    """
    from types import SimpleNamespace

    from platforms.chatgpt.status_probe import probe_local_chatgpt_status
    from services.chatgpt_sync import update_account_model_local_probe

    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is None:
            return ""
        extra = row.get_extra()
        source = SimpleNamespace(
            platform="chatgpt",
            email=row.email,
            password=row.password,
            token=row.token,
            user_id=row.user_id,
            extra=dict(extra),
            access_token=str(extra.get("access_token") or ""),
            refresh_token=str(extra.get("refresh_token") or ""),
            id_token=str(extra.get("id_token") or ""),
            session_token=str(extra.get("session_token") or ""),
            client_id=str(extra.get("client_id") or ""),
            cookies=str(extra.get("cookies") or ""),
        )
    try:
        probe = probe_local_chatgpt_status(source, proxy=proxy)
    except Exception as exc:  # noqa: BLE001 - 网络异常不应回滚已保存的凭证
        # 测活内部可能已经刷新到新 AT/RT，异常时也要单独补存，别让轮换丢失。
        try:
            from services.chatgpt_sync import merge_account_credentials

            merge_account_credentials(account_id, source)
        except Exception:
            pass
        return type(exc).__name__
    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is not None:
            update_account_model_local_probe(
                row, probe, session=session, commit=True, credential_source=source
            )
    return ""


def supplement_account(account: AccountModel, settings: SmsStockSettings, *, proxy, control, attempt_id, log) -> bool:
    from services.chatgpt_rt_backfill import apply_backfill_result, backfill_account_data

    from core.resource_limits import account_slot

    with account_slot(control, attempt_id), get_account_lock(account.id):
        _mark_attempt(account.id)
        config = dict(config_store.get_all() or {})
        result = backfill_account_data(
            email=account.email, password=account.password, extra=account.get_extra(), token=account.token,
            config=config, proxy=proxy, allow_login=settings.allow_login, log_fn=log,
            task_control=control, attempt_id=attempt_id,
        )
        # 已到手的凭证必须先落库，再响应停止请求；短信事实随结果一起保存。
        with Session(engine) as session:
            row = session.get(AccountModel, account.id)
            if row is None:
                raise ValueError("账号已被删除，无法保存接库结果")
            apply_backfill_result(row, result, session=session, commit=True)

        probe_error = ""
        if result.success:
            control.checkpoint(attempt_id=attempt_id)
            probe_error = _probe_after_backfill(account.id, proxy, log)
            if probe_error:
                log(f"[补库] 后置测活异常: {probe_error}，已保留新凭证，留待下次验证")

        with Session(engine) as session:
            row = session.get(AccountModel, account.id)
        ok = bool(row is not None and _in_stock(row))
        if ok:
            message = "RT 已就绪" + ("（含短信验证记录）" if result.sms_verification else "")
        elif not result.success:
            message = f"补 RT 失败: {result.summary()[:160]}"
        elif probe_error:
            message = f"后置测活异常: {probe_error}"
        else:
            message = "凭证测活未通过"
        _mark_attempt(account.id, ok=ok, message=message)
        control.checkpoint(attempt_id=attempt_id)
        if not ok:
            log(f"[FAIL] 未取得可用的 RT：{message}")
        return ok


@dataclass
class StockRunResult:
    attempts: int = 0
    success: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    reason: str = ""
    final_inventory: int = 0


def replenish(settings: SmsStockSettings, *, control, log: Callable, register: Callable, progress: Callable, should_continue: Callable) -> StockRunResult:
    """串行、有界地补足缺口；每次有副作用前重新统计，不按最初缺口盲目造号。"""
    from core.proxy_pool import proxy_pool
    from core.proxy_utils import normalize_proxy_url

    result = StockRunResult()
    attempted: set[int] = set()
    failures = 0
    pause = threading.Event()
    while result.attempts < settings.max_attempts_per_run:
        control.checkpoint()
        if not should_continue():
            result.reason = "自动补库已关闭，不再启动新的付费操作"
            break
        stock = inventory(settings)
        if stock["missing_accounts"] == 0:
            result.reason = "已达到目标库存"
            break
        if failures >= settings.max_consecutive_failures:
            result.reason = "连续失败达到上限，停止本轮补库"
            break
        candidate = _next_candidate(settings, attempted)
        if not reserve_attempt(settings):
            result.reason = "已达到每日尝试上限（UTC）"
            break
        result.attempts += 1
        phase = "补已有账号接码" if candidate else "注册并接码"
        progress(result, phase)
        attempt_id = control.start_attempt() if candidate else None
        try:
            if candidate:
                attempted.add(candidate.id)
                log(f"[补库] 优先处理已注册未接码账号 #{candidate.id}: {candidate.email}")
                ok = supplement_account(
                    candidate, settings, proxy=normalize_proxy_url(proxy_pool.get_next()),
                    control=control, attempt_id=attempt_id, log=log,
                )
            else:
                log("[补库] 无可尝试的未接码账号，执行注册＋接码")
                ok = register(settings, control)
            if ok:
                result.success += 1
                failures = 0
                log("[OK] 已补充 1 个可用 RT 账号")
            else:
                failures += 1
                result.errors.append(f"第 {result.attempts} 次{phase}未取得可用 RT")
        except StopTaskRequested:
            raise
        except SkipCurrentAttemptRequested:
            result.skipped += 1
            log("[SKIP] 已跳过当前补库尝试")
        except Exception as exc:
            failures += 1
            result.errors.append(f"{phase}异常: {type(exc).__name__}")
            log(f"[FAIL] {phase}异常: {type(exc).__name__}")
        finally:
            if attempt_id is not None:
                control.finish_attempt(attempt_id)
            progress(result, phase)
        if inventory(settings)["missing_accounts"] == 0:
            result.reason = "已达到目标库存"
            break
        if result.attempts < settings.max_attempts_per_run:
            deadline = time.monotonic() + settings.delay_seconds
            while time.monotonic() < deadline:
                control.checkpoint()
                pause.wait(min(0.25, max(0, deadline - time.monotonic())))
    result.final_inventory = inventory(settings)["verified_accounts"]
    result.reason = result.reason or "已达到每轮尝试上限，余量留待下轮补充"
    return result
