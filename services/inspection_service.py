"""可配置的 ChatGPT 账号巡检服务。

巡检把「状态探测、缺 RT 补齐、外部上传」拆成可独立开关的步骤，
并且只接收账号快照，不在长时间网络请求期间持有数据库连接。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

import requests
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, engine
from core.task_runtime import AttemptResult, SkipCurrentAttemptRequested, StopTaskRequested

logger = logging.getLogger(__name__)

INSPECTION_CONFIG_KEYS = {
    "inspection_enabled",
    "inspection_interval_minutes",
    "inspection_concurrency",
    "inspection_delay_seconds",
    "inspection_probe_enabled",
    "inspection_backfill_rt_enabled",
    "inspection_backfill_allow_login",
    "inspection_only_missing_rt",
    "inspection_only_sms_verified",
    "inspection_upload_cpa",
    "inspection_upload_sub2api",
    "inspection_chat2api_enabled",
    "inspection_chat2api_protocol",
    "inspection_chat2api_url",
    "inspection_chat2api_token",
    "inspection_account_statuses",
    "inspection_email_filter",
    "inspection_max_accounts",
}

DEFAULT_STATUSES = ("registered", "trial", "subscribed")
ALLOWED_STATUSES = {
    "registered",
    "trial",
    "subscribed",
    "invalid",
    "expired",
    "deleted",
    "banned",
}


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "enabled"}


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _parse_statuses(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or value is None:
        raw_items = _text(value).replace("，", ",").split(",")
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        raw_items = value
    else:
        raw_items = _text(value).replace("，", ",").split(",")
    result: list[str] = []
    for item in raw_items:
        status = _text(item).lower()
        if status in ALLOWED_STATUSES and status not in result:
            result.append(status)
    return tuple(result or DEFAULT_STATUSES)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_chat2api_protocol(value: Any) -> str:
    protocol = _text(value).lower()
    return protocol if protocol in {"chatgpt2api", "webhook"} else "chatgpt2api"


@dataclass(frozen=True)
class InspectionSettings:
    enabled: bool = False
    interval_minutes: int = 60
    concurrency: int = 1
    delay_seconds: float = 5.0
    probe_enabled: bool = True
    backfill_rt_enabled: bool = False
    backfill_allow_login: bool = False
    only_missing_rt: bool = True
    only_sms_verified: bool = False
    upload_cpa: bool = False
    upload_sub2api: bool = False
    chat2api_enabled: bool = False
    chat2api_protocol: str = "chatgpt2api"
    chat2api_url: str = ""
    chat2api_token: str = ""
    account_statuses: tuple[str, ...] = DEFAULT_STATUSES
    email_filter: str = ""
    max_accounts: int = 0

    @classmethod
    def from_store(cls) -> "InspectionSettings":
        return cls(
            enabled=_bool(config_store.get("inspection_enabled", ""), False),
            interval_minutes=_int(config_store.get("inspection_interval_minutes", ""), 60, 1, 10080),
            concurrency=_int(config_store.get("inspection_concurrency", ""), 1, 1, 20),
            delay_seconds=_float(config_store.get("inspection_delay_seconds", ""), 5.0, 0.0, 3600.0),
            probe_enabled=_bool(config_store.get("inspection_probe_enabled", ""), True),
            backfill_rt_enabled=_bool(config_store.get("inspection_backfill_rt_enabled", ""), False),
            backfill_allow_login=_bool(config_store.get("inspection_backfill_allow_login", ""), False),
            only_missing_rt=_bool(config_store.get("inspection_only_missing_rt", ""), True),
            only_sms_verified=_bool(config_store.get("inspection_only_sms_verified", ""), False),
            upload_cpa=_bool(config_store.get("inspection_upload_cpa", ""), False),
            upload_sub2api=_bool(config_store.get("inspection_upload_sub2api", ""), False),
            chat2api_enabled=_bool(config_store.get("inspection_chat2api_enabled", ""), False),
            chat2api_protocol=_normalize_chat2api_protocol(config_store.get("inspection_chat2api_protocol", "")),
            chat2api_url=_text(config_store.get("inspection_chat2api_url", "")),
            chat2api_token=_text(config_store.get("inspection_chat2api_token", "")),
            account_statuses=_parse_statuses(config_store.get("inspection_account_statuses", "")),
            email_filter=_text(config_store.get("inspection_email_filter", "")),
            max_accounts=_int(config_store.get("inspection_max_accounts", ""), 0, 0, 5000),
        )

    def with_overrides(self, overrides: dict[str, Any] | None = None) -> "InspectionSettings":
        if not overrides:
            return self
        values: dict[str, Any] = {}
        for name in (
            "enabled",
            "interval_minutes",
            "concurrency",
            "delay_seconds",
            "probe_enabled",
            "backfill_rt_enabled",
            "backfill_allow_login",
            "only_missing_rt",
            "only_sms_verified",
            "upload_cpa",
            "upload_sub2api",
            "chat2api_enabled",
            "chat2api_protocol",
            "chat2api_url",
            "chat2api_token",
            "account_statuses",
            "email_filter",
            "max_accounts",
        ):
            if name in overrides and overrides[name] is not None:
                values[name] = overrides[name]
        if "account_statuses" in values:
            values["account_statuses"] = _parse_statuses(values["account_statuses"])
        if "interval_minutes" in values:
            values["interval_minutes"] = _int(values["interval_minutes"], self.interval_minutes, 1, 10080)
        if "concurrency" in values:
            values["concurrency"] = _int(values["concurrency"], self.concurrency, 1, 20)
        if "delay_seconds" in values:
            values["delay_seconds"] = _float(values["delay_seconds"], self.delay_seconds, 0.0, 3600.0)
        if "max_accounts" in values:
            values["max_accounts"] = _int(values["max_accounts"], self.max_accounts, 0, 5000)
        for name in (
            "enabled",
            "probe_enabled",
            "backfill_rt_enabled",
            "backfill_allow_login",
            "only_missing_rt",
            "only_sms_verified",
            "upload_cpa",
            "upload_sub2api",
            "chat2api_enabled",
        ):
            if name in values:
                values[name] = _bool(values[name], getattr(self, name))
        if "chat2api_protocol" in values:
            values["chat2api_protocol"] = _normalize_chat2api_protocol(values["chat2api_protocol"])
        if "chat2api_url" in values:
            values["chat2api_url"] = _text(values["chat2api_url"])
        if "chat2api_token" in values:
            values["chat2api_token"] = _text(values["chat2api_token"])
        if "email_filter" in values:
            values["email_filter"] = _text(values["email_filter"])
        return replace(self, **values)

    def public_dict(self) -> dict[str, Any]:
        token = self.chat2api_token
        return {
            "enabled": self.enabled,
            "interval_minutes": self.interval_minutes,
            "concurrency": self.concurrency,
            "delay_seconds": self.delay_seconds,
            "probe_enabled": self.probe_enabled,
            "backfill_rt_enabled": self.backfill_rt_enabled,
            "backfill_allow_login": self.backfill_allow_login,
            "only_missing_rt": self.only_missing_rt,
            "only_sms_verified": self.only_sms_verified,
            "upload_cpa": self.upload_cpa,
            "upload_sub2api": self.upload_sub2api,
            "chat2api_enabled": self.chat2api_enabled,
            "chat2api_protocol": self.chat2api_protocol,
            "chat2api_url": self.chat2api_url,
            "chat2api_token_configured": bool(token),
            "chat2api_token_masked": (f"{token[:3]}***{token[-3:]}" if len(token) >= 7 else ("***" if token else "")),
            "account_statuses": list(self.account_statuses),
            "email_filter": self.email_filter,
            "max_accounts": self.max_accounts,
        }


def update_settings(data: dict[str, Any]) -> InspectionSettings:
    """更新巡检配置，空的 secret 不会覆盖已有 token。"""
    safe: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key not in INSPECTION_CONFIG_KEYS:
            continue
        if key == "inspection_chat2api_token" and not _text(value):
            continue
        if key in {
            "inspection_enabled",
            "inspection_probe_enabled",
            "inspection_backfill_rt_enabled",
            "inspection_backfill_allow_login",
            "inspection_only_missing_rt",
            "inspection_only_sms_verified",
            "inspection_upload_cpa",
            "inspection_upload_sub2api",
            "inspection_chat2api_enabled",
        }:
            safe[key] = "1" if _bool(value) else "0"
        elif key == "inspection_account_statuses":
            safe[key] = ",".join(_parse_statuses(value))
        elif key == "inspection_interval_minutes":
            safe[key] = str(_int(value, 60, 1, 10080))
        elif key == "inspection_concurrency":
            safe[key] = str(_int(value, 1, 1, 20))
        elif key == "inspection_delay_seconds":
            safe[key] = str(_float(value, 5.0, 0.0, 3600.0))
        elif key == "inspection_max_accounts":
            safe[key] = str(_int(value, 0, 0, 5000))
        elif key == "inspection_chat2api_protocol":
            safe[key] = _normalize_chat2api_protocol(value)
        else:
            safe[key] = _text(value)
    if safe:
        config_store.set_many(safe)
    return InspectionSettings.from_store()


def select_account_ids(
    *,
    account_ids: Iterable[int] | None = None,
    statuses: Iterable[str] | None = None,
    email_filter: str = "",
    max_accounts: int = 0,
    only_sms_verified: bool = False,
) -> tuple[list[int], list[int]]:
    """返回 (匹配账号 ID, 请求中不存在的 ID)。"""
    requested: list[int] = []
    seen: set[int] = set()
    for raw in account_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in seen:
            requested.append(value)
            seen.add(value)
    if statuses is None:
        wanted_statuses = DEFAULT_STATUSES
    elif isinstance(statuses, Iterable) and not isinstance(statuses, (str, bytes, bytearray, dict)):
        status_items = list(statuses)
        if not status_items:
            return [], requested
        wanted_statuses = _parse_statuses(status_items)
    else:
        wanted_statuses = _parse_statuses(statuses)
    needle = _text(email_filter).lower()
    with Session(engine) as session:
        query = select(AccountModel).where(AccountModel.platform == "chatgpt")
        if wanted_statuses:
            query = query.where(AccountModel.status.in_(list(wanted_statuses)))
        if requested:
            query = query.where(AccountModel.id.in_(requested))
        rows = session.exec(query.order_by(AccountModel.id)).all()
    # Web 反代号不参与 Codex 测活/补 RT/接码；它们由远端号池额度单独管理。
    from services.chatgpt_account_purpose import is_web_pool_account

    rows = [row for row in rows if not is_web_pool_account(row)]
    if needle:
        rows = [row for row in rows if needle in _text(row.email).lower()]
    if only_sms_verified:
        from services.chatgpt_account_state import account_sms_verified

        rows = [row for row in rows if account_sms_verified(row)]
    if max_accounts > 0:
        rows = rows[:max_accounts]
    found = [int(row.id) for row in rows if row.id is not None]
    missing = [value for value in requested if value not in found]
    return found, missing


def _account_object(fields: dict[str, Any]) -> Any:
    extra = fields.get("extra") if isinstance(fields.get("extra"), dict) else {}
    access_token = _text(extra.get("access_token") or extra.get("accessToken") or fields.get("token"))
    refresh_token = _text(extra.get("refresh_token") or extra.get("refreshToken"))
    id_token = _text(extra.get("id_token") or extra.get("idToken"))
    session_token = _text(extra.get("session_token") or extra.get("sessionToken"))
    client_id = _text(extra.get("client_id") or extra.get("clientId"))
    return SimpleNamespace(
        platform="chatgpt",
        email=_text(fields.get("email")),
        password=_text(fields.get("password")),
        token=_text(fields.get("token")),
        user_id=_text(fields.get("user_id")),
        extra=dict(extra),
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        session_token=session_token,
        client_id=client_id,
        cookies=_text(extra.get("cookies")),
    )


def _load_current_account(account_id: int) -> AccountModel | None:
    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is None:
            return None
        # 网络操作不能持有 ORM session；复制成一个脱离 session 的快照。
        copy = AccountModel(
            id=row.id,
            platform=row.platform,
            email=row.email,
            password=row.password,
            user_id=row.user_id,
            region=row.region,
            token=row.token,
            status=row.status,
            trial_end_time=row.trial_end_time,
            cashier_url=row.cashier_url,
            extra_json=row.extra_json,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        return copy


def _persist_probe(account_id: int, probe: dict[str, Any], credential_source: Any = None) -> None:
    from services.chatgpt_sync import update_account_model_local_probe

    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is not None:
            update_account_model_local_probe(
                row,
                probe,
                session=session,
                commit=True,
                credential_source=credential_source,
            )


def _persist_sync_result(account_id: int, provider: str, ok: bool, message: str) -> None:
    from services.chatgpt_sync import (
        update_account_model_cpa_sync,
        update_account_model_sub2api_sync,
    )

    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is None:
            return
        if provider == "cpa":
            update_account_model_cpa_sync(row, ok, message, session=session, commit=True)
        else:
            update_account_model_sub2api_sync(row, ok, message, session=session, commit=True)


def _validate_webhook_url(value: str) -> str:
    url = _text(value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Chat2API 地址必须是 http:// 或 https:// URL")
    return url


def _upload_chat2api(account: Any, probe: dict[str, Any], settings: InspectionSettings) -> tuple[bool, str]:
    url = _validate_webhook_url(settings.chat2api_url)
    extra = account.extra if isinstance(getattr(account, "extra", None), dict) else {}
    from platforms.chatgpt.status_probe import extract_chatgpt_account_id

    payload = {
        "platform": "chatgpt",
        "email": _text(getattr(account, "email", "")),
        "user_id": _text(getattr(account, "user_id", "")),
        "chatgpt_account_id": extract_chatgpt_account_id(account),
        "access_token": _text(getattr(account, "access_token", "") or extra.get("access_token") or extra.get("accessToken") or getattr(account, "token", "")),
        "refresh_token": _text(getattr(account, "refresh_token", "") or extra.get("refresh_token") or extra.get("refreshToken")),
        "id_token": _text(getattr(account, "id_token", "") or extra.get("id_token") or extra.get("idToken")),
        "session_token": _text(getattr(account, "session_token", "") or extra.get("session_token") or extra.get("sessionToken")),
        "client_id": _text(getattr(account, "client_id", "") or extra.get("client_id") or extra.get("clientId")),
        "source": "any-auto-register-inspection",
        "checked_at": _iso_now(),
        "probe": {
            "auth": (probe.get("auth") if isinstance(probe, dict) else {}) or {},
            "subscription": (probe.get("subscription") if isinstance(probe, dict) else {}) or {},
            "codex": (probe.get("codex") if isinstance(probe, dict) else {}) or {},
        },
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.chat2api_token:
        headers["Authorization"] = f"Bearer {settings.chat2api_token}"
        headers["X-API-Key"] = settings.chat2api_token
    try:
        request_payload: Any
        if settings.chat2api_protocol == "chatgpt2api":
            # chatgpt2api 的管理接口接收批量账号包装；保留可配置 URL，
            # 便于部署在反向代理或自定义管理路径下。
            request_payload = {"accounts": [payload]}
        else:
            # webhook 协议直接发送单个账号快照。
            request_payload = payload
        response = requests.post(url, json=request_payload, headers=headers, timeout=30)
    except requests.RequestException as exc:
        return False, f"Chat2API 请求失败: {type(exc).__name__}"
    if 200 <= response.status_code < 300:
        return True, f"HTTP {response.status_code}"
    return False, f"Chat2API 返回 HTTP {response.status_code}"


def _probe_states(probe: dict[str, Any]) -> tuple[str, str]:
    return (
        _text((probe.get("auth") or {}).get("state")),
        _text((probe.get("codex") or {}).get("state")),
    )


def _probe_recoverable(probe: dict[str, Any]) -> bool:
    auth, codex = _probe_states(probe)
    return auth in {"unauthorized", "access_token_invalidated", "missing_access_token"} or (
        auth == "access_token_valid" and codex in {"unauthorized", "access_token_invalidated"}
    )


def _probe_uploadable(probe: dict[str, Any]) -> bool:
    auth, codex = _probe_states(probe)
    return auth == "access_token_valid" and codex not in {
        "probe_failed", "unauthorized", "access_token_invalidated", "account_deactivated",
        "skipped_auth_invalid", "not_checked", "",
    }


def _backfill_and_persist(account_id, current, settings, proxy, log_fn, task_control, attempt_id):
    """写路径复用同一份补 RT 结果，恢复认证后必须另行重测。"""
    from services.chatgpt_rt_backfill import apply_backfill_result, backfill_account_data

    result = backfill_account_data(
        email=current.email,
        password=current.password,
        extra=current.get_extra(),
        token=current.token,
        config=config_store.get_all() or {},
        proxy=proxy,
        allow_login=settings.backfill_allow_login,
        log_fn=log_fn,
        task_control=task_control,
        attempt_id=attempt_id,
    )
    # 先落库再响应停止：补 RT 可能已经拿到新凭证并完成短信验证，
    # 此时用户点停止也不应该把成果一起丢掉。
    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is None:
            raise ValueError("账号已被删除，无法保存补 RT 结果")
        apply_backfill_result(row, result, session=session, commit=True)
    if task_control is not None:
        task_control.checkpoint(attempt_id=attempt_id)
    return result


def inspect_account(
    *,
    account_id: int,
    fields: dict[str, Any],
    settings: InspectionSettings,
    proxy: Optional[str],
    log_fn: Callable[[str], None],
    task_control: Any = None,
    attempt_id: Any = None,
) -> AttemptResult:
    """执行一个账号的巡检步骤（同账号串行，避免与手动补 RT 并发轮换凭证）。"""
    from services.account_locks import get_account_lock

    with get_account_lock(account_id):
        return _inspect_account_impl(
            account_id=account_id,
            fields=fields,
            settings=settings,
            proxy=proxy,
            log_fn=log_fn,
            task_control=task_control,
            attempt_id=attempt_id,
        )


def _inspect_account_impl(
    *,
    account_id: int,
    fields: dict[str, Any],
    settings: InspectionSettings,
    proxy: Optional[str],
    log_fn: Callable[[str], None],
    task_control: Any = None,
    attempt_id: Any = None,
) -> AttemptResult:
    account = _account_object(fields)
    operational_errors: list[str] = []
    probe: dict[str, Any] = {}
    probe_failed = False
    probe_error = ""

    def checkpoint():
        if task_control is not None:
            task_control.checkpoint(attempt_id=attempt_id)

    def measure(source):
        nonlocal probe, probe_failed, probe_error
        from platforms.chatgpt.status_probe import probe_local_chatgpt_status

        checkpoint()
        try:
            probe = probe_local_chatgpt_status(source, proxy=proxy)
            _persist_probe(account_id, probe, credential_source=source)
            auth, codex = _probe_states(probe)
            detail = probe.get("auth") or {}
            log_fn(f"[测活] auth={auth}, codex={codex}, HTTP={detail.get('http_status', 0)}, error={detail.get('error_code') or '-'}")
            for refresh in detail.get("refresh_attempts") or []:
                log_fn(f"[刷新] {refresh.get('strategy')}: {'已换取 AT' if refresh.get('ok') else refresh.get('error', '失败')}")
            checkpoint()
        except (SkipCurrentAttemptRequested, StopTaskRequested):
            raise
        except Exception as exc:
            probe_failed = True
            probe_error = type(exc).__name__
            # 刷新可能已经成功轮到新凭证；异常会跳出正常保存路径，这里单独补存。
            try:
                from services.chatgpt_sync import merge_account_credentials

                merge_account_credentials(account_id, source)
            except Exception:
                pass
            log_fn(f"[FAIL] 测活异常: {type(exc).__name__}")

    if settings.probe_enabled:
        measure(account)
        if probe_failed:
            return AttemptResult.failed("测活异常，未执行后续巡检动作")
        if _probe_recoverable(probe):
            log_fn("[恢复] 认证暂未通过，使用最新凭证重测一次")
            current = _load_current_account(account_id)
            if current is None:
                return AttemptResult.failed(f"账号 #{account_id} 不存在")
            measure(_account_object({"email": current.email, "token": current.token, "user_id": current.user_id, "extra": current.get_extra()}))
            if probe_failed:
                return AttemptResult.failed("认证重测异常，未执行上传")

    current = _load_current_account(account_id)
    if current is None:
        return AttemptResult.failed(f"账号 #{account_id} 不存在")
    auth_blocked = settings.probe_enabled and not _probe_uploadable(probe)
    recoverable = auth_blocked and _probe_recoverable(probe)
    if auth_blocked and not (recoverable and settings.backfill_rt_enabled):
        auth, codex = _probe_states(probe)
        log_fn(f"[FAIL] 认证未通过: auth={auth}, codex={codex}，未执行上传")
        return AttemptResult.failed(f"认证未通过: auth={auth}, codex={codex}")

    if settings.backfill_rt_enabled:
        from services.chatgpt_rt_backfill import account_missing_rt

        # 仅缺 RT 是日常补齐筛选，不能阻断失效凭证的显式恢复。
        if settings.only_missing_rt and not account_missing_rt(current) and not recoverable:
            log_fn("[补 RT] 已有 RT，跳过")
        else:
            try:
                checkpoint()
                if recoverable:
                    log_fn("[恢复] 重测仍未通过，尝试补 RT／按设置重新登录")
                result = _backfill_and_persist(account_id, current, settings, proxy, log_fn, task_control, attempt_id)
                if result.account_deleted:
                    return AttemptResult.failed("账号已明确删除或停用，停止恢复和上传")
                if result.success:
                    log_fn(f"[补 RT] 成功（{result.strategy or 'unknown'}）")
                else:
                    operational_errors.append("补 RT 失败")
                    log_fn(f"[FAIL] 补 RT 失败: {result.summary()[:200]}")
            except (SkipCurrentAttemptRequested, StopTaskRequested):
                raise
            except Exception as exc:
                operational_errors.append(f"补 RT 异常: {type(exc).__name__}")
                log_fn(f"[FAIL] 补 RT 异常: {type(exc).__name__}")
            if settings.probe_enabled:
                latest = _load_current_account(account_id)
                if latest is None:
                    return AttemptResult.failed(f"账号 #{account_id} 不存在")
                log_fn("[恢复] 使用补 RT 后的最新凭证重新测活")
                measure(_account_object({"email": latest.email, "token": latest.token, "user_id": latest.user_id, "extra": latest.get_extra()}))
                if probe_failed:
                    # 不能拿第一次的旧测活结果解释这次失败，否则会显示
                    # “auth=access_token_valid, codex=usable 未通过”这种自相矛盾的提示。
                    log_fn(f"[FAIL] 后置测活异常: {probe_error or 'error'}，结果待确认，未执行上传")
                    return AttemptResult.failed(f"后置测活异常: {probe_error or 'error'}")
                if not _probe_uploadable(probe):
                    auth, codex = _probe_states(probe)
                    log_fn(f"[FAIL] 恢复后仍未通过认证: auth={auth}, codex={codex}，未执行上传")
                    return AttemptResult.failed(f"恢复后认证未通过: auth={auth}, codex={codex}")
                auth_blocked = False

    if task_control is not None:
        task_control.checkpoint(attempt_id=attempt_id)

    # 补 RT 可能更新了 access_token/refresh_token，上传前重新取一份快照。
    current = _load_current_account(account_id) or current
    upload_account = _account_object(
        {
            "email": current.email,
            "password": current.password,
            "token": current.token,
            "user_id": current.user_id,
            "extra": current.get_extra(),
        }
    )

    if settings.upload_cpa and not auth_blocked:
        if task_control is not None:
            task_control.checkpoint(attempt_id=attempt_id)
        try:
            from services.chatgpt_sync import upload_chatgpt_account_to_cpa

            url = _text(config_store.get("cpa_api_url", ""))
            key = _text(config_store.get("cpa_api_key", ""))
            ok, msg = upload_chatgpt_account_to_cpa(upload_account, api_url=url, api_key=key)
            _persist_sync_result(account_id, "cpa", ok, msg)
            log_fn(f"[CPA] {'成功' if ok else '失败'}: {msg[:200]}")
            if not ok:
                operational_errors.append("CPA 上传失败")
            if task_control is not None:
                task_control.checkpoint(attempt_id=attempt_id)
        except (SkipCurrentAttemptRequested, StopTaskRequested):
            raise
        except Exception as exc:  # noqa: BLE001
            operational_errors.append(f"CPA 上传异常: {type(exc).__name__}")
            log_fn(f"[FAIL] CPA 上传异常: {type(exc).__name__}")

    if settings.upload_sub2api and not auth_blocked:
        if task_control is not None:
            task_control.checkpoint(attempt_id=attempt_id)
        try:
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            url = _text(config_store.get("sub2api_api_url", ""))
            key = _text(config_store.get("sub2api_api_key", ""))
            ok, msg = upload_to_sub2api(upload_account, api_url=url, api_key=key)
            _persist_sync_result(account_id, "sub2api", ok, msg)
            log_fn(f"[Sub2API] {'成功' if ok else '失败'}: {msg[:200]}")
            if not ok:
                operational_errors.append("Sub2API 上传失败")
            if task_control is not None:
                task_control.checkpoint(attempt_id=attempt_id)
        except (SkipCurrentAttemptRequested, StopTaskRequested):
            raise
        except Exception as exc:  # noqa: BLE001
            operational_errors.append(f"Sub2API 上传异常: {type(exc).__name__}")
            log_fn(f"[FAIL] Sub2API 上传异常: {type(exc).__name__}")

    if settings.chat2api_enabled and not auth_blocked:
        if task_control is not None:
            task_control.checkpoint(attempt_id=attempt_id)
        try:
            ok, msg = _upload_chat2api(upload_account, probe, settings)
            log_fn(f"[Chat2API] {'成功' if ok else '失败'}: {msg}")
            if not ok:
                operational_errors.append("Chat2API 上传失败")
            if task_control is not None:
                task_control.checkpoint(attempt_id=attempt_id)
        except (SkipCurrentAttemptRequested, StopTaskRequested):
            raise
        except Exception as exc:  # noqa: BLE001
            operational_errors.append(f"Chat2API 上传异常: {type(exc).__name__}")
            log_fn(f"[FAIL] Chat2API 上传异常: {type(exc).__name__}")

    if operational_errors:
        return AttemptResult.failed("；".join(operational_errors))
    return AttemptResult.success()
