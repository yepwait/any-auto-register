"""chatgpt2api Web pool: authenticated quota reads and bounded, SMS-free refill.

Contract: GET/POST /api/accounts in the local chatgpt2api deployment. Quota is
image quota, not Codex quota. No refresh, re-login, deletion or generation calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
from typing import Callable, Literal
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, engine
from core.task_runtime import SkipCurrentAttemptRequested, StopTaskRequested
from services.account_locks import get_account_lock
from services.chatgpt_account_purpose import is_web_pool_account

SOURCE = "chatgpt_web_pool"
REGISTER_SOURCE = "chatgpt_web_pool_register"
PREFIX = "chatgpt_web_pool_"
_budget_lock = threading.Lock()


class PoolError(ValueError):
    """Messages must be safe for logs: no response bodies, tokens or URLs."""


class WebPoolSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = ""
    admin_token: str = Field(default="", repr=False)
    target_mode: Literal["quota", "available_accounts"] = "quota"
    target_value: int = Field(default=100, ge=1, le=1000000)
    interval_minutes: int = Field(default=30, ge=1, le=10080)
    max_attempts_per_run: int = Field(default=5, ge=1, le=100)
    daily_registration_limit: int = Field(default=20, ge=0, le=1000)  # 0 = no daily cap
    max_consecutive_failures: int = Field(default=3, ge=1, le=10)
    delay_seconds: float = Field(default=5, ge=0, le=3600, allow_inf_nan=False)
    request_timeout: int = Field(default=60, ge=5, le=180)

    @field_validator("base_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        try:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError
            if parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError
            _ = parts.port
        except ValueError:
            raise ValueError("请填写不含用户名、密码、查询参数的 HTTP(S) 服务根地址") from None
        if parts.path.endswith(("/api/accounts", "/health")):
            raise ValueError("请填写服务根地址（可含反代前缀），不要填写 /api/accounts 或 /health")
        return value

    @field_validator("admin_token")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        value = value.strip()
        if "\r" in value or "\n" in value:
            raise ValueError("管理员密钥不能包含换行")
        return value

    @classmethod
    def from_store(cls):
        return cls.model_validate({
            key: config_store.get(PREFIX + key, str(item.default))
            for key, item in cls.model_fields.items()
        })

    def public_dict(self) -> dict:
        return {**self.model_dump(exclude={"admin_token"}), "admin_token_configured": bool(self.admin_token)}

    def validate_connection(self) -> None:
        if not self.base_url or not self.admin_token:
            raise PoolError("请先配置 chatgpt2api 服务根地址和管理员密钥（不是普通用户 API Key）")

    @property
    def target_id(self) -> str:
        return hashlib.sha256(self.base_url.encode()).hexdigest()


def save_settings(settings: WebPoolSettings) -> None:
    config_store.set_many({
        PREFIX + key: ("1" if value else "0") if isinstance(value, bool) else str(value)
        for key, value in settings.model_dump().items()
    })


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def daily_used() -> int:
    if config_store.get(PREFIX + "budget_date", "") != today():
        return 0
    return max(0, int(config_store.get(PREFIX + "budget_used", "0")))


def reserve_registration(settings: WebPoolSettings) -> bool:
    with _budget_lock:
        used = daily_used()
        if settings.daily_registration_limit > 0 and used >= settings.daily_registration_limit:
            return False
        config_store.set_many({PREFIX + "budget_date": today(), PREFIX + "budget_used": str(used + 1)})
        return True


def _quota(value) -> int:
    # Unknown/missing quota must never silently become zero and trigger spending.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PoolError("远端额度字段无效，停止补号；请检查 chatgpt2api 版本")
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        raise PoolError("远端额度字段无效，停止补号；请检查 chatgpt2api 版本") from None


def parse_items(data) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise PoolError("远端未返回 accounts.items，停止补号；请检查地址、鉴权及版本")
    items = data["items"]
    for item in items:
        if not isinstance(item, dict) or item.get("status") not in {"正常", "限流", "异常", "禁用"}:
            raise PoolError("远端账号状态格式不兼容，停止补号")
        if item.get("status") == "正常" and item.get("image_quota_unknown"):
            raise PoolError("远端存在图片额度未知的正常账号，停止自动补号；请先在远端确认额度")
        _quota(item.get("quota"))
    return items


def summarize(items: list[dict]) -> dict:
    normal = [item for item in items if item["status"] == "正常"]
    return {
        "total_accounts": len(items),
        "normal_accounts": len(normal),
        "available_accounts": sum(_quota(item["quota"]) > 0 for item in normal),
        "remaining_quota": sum(_quota(item["quota"]) for item in normal),
        "limited_accounts": sum(item["status"] == "限流" for item in items),
        "abnormal_accounts": sum(item["status"] == "异常" for item in items),
        "disabled_accounts": sum(item["status"] == "禁用" for item in items),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


class WebPoolClient:
    def __init__(self, settings: WebPoolSettings):
        settings.validate_connection()
        self.settings = settings

    def _request(self, method: str, payload=None) -> dict:
        # Never follow redirects with account credentials. Ignore process-level
        # proxy/netrc: the explicitly configured management server is the target.
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(
                    method, self.settings.base_url + "/api/accounts",
                    headers={"Authorization": f"Bearer {self.settings.admin_token}", "Accept": "application/json"},
                    json=payload, timeout=self.settings.request_timeout, allow_redirects=False,
                )
                if response.status_code in {401, 403}:
                    raise PoolError("chatgpt2api 管理员鉴权失败，未执行后续注册")
                if not 200 <= response.status_code < 300:
                    raise PoolError(f"chatgpt2api 返回 HTTP {response.status_code}，停止本轮")
                try:
                    data = response.json()
                except ValueError:
                    raise PoolError("chatgpt2api 返回非 JSON（可能是登录页），停止本轮") from None
                if not isinstance(data, dict) or data.get("error") or data.get("detail") or data.get("success") is False:
                    raise PoolError("chatgpt2api 返回业务错误，停止本轮")
                return data
        except requests.RequestException as exc:
            raise PoolError(f"chatgpt2api 请求失败（{type(exc).__name__}），保留本地账号待重试") from None

    def snapshot(self) -> dict:
        return summarize(parse_items(self._request("GET")))

    @staticmethod
    def _confirmed_item(item: dict, refresh_errors: int = 0) -> dict:
        ready = item["status"] == "正常" and _quota(item["quota"]) > 0 and not refresh_errors
        return {"imported": True, "ready": ready, "status": item["status"], "quota": _quota(item["quota"]), "refresh_errors": refresh_errors}

    @staticmethod
    def _match_item(items: list[dict], account: AccountModel, token: str) -> dict | None:
        matched = next((item for item in items if item.get("access_token") == token), None)
        if matched is not None:
            return matched
        # The remote refresh can rotate AT. Use a stable user/account id when
        # available, and require it together with the submitted email; email
        # alone is only a display hint and cannot confirm a rotated credential.
        user_id = str(account.user_id or account.get_extra().get("user_id") or "").strip()
        email = str(account.email or "").strip().lower()
        if not user_id or not email:
            return None
        return next((item for item in items if str(item.get("user_id") or "").strip() == user_id and str(item.get("email") or "").strip().lower() == email), None)

    def find_imported(self, account: AccountModel) -> dict | None:
        """Reconcile uncertain POSTs by token or stable identity before reimport."""
        extra = account.get_extra()
        token = str(extra.get("access_token") or extra.get("accessToken") or account.token or "").strip()
        items = parse_items(self._request("GET"))
        matched = self._match_item(items, account, token)
        return self._confirmed_item(matched) if matched is not None else None

    def upload(self, account: AccountModel) -> dict:
        extra = account.get_extra()
        token = str(extra.get("access_token") or extra.get("accessToken") or account.token or "").strip()
        if not token:
            raise PoolError("本地账号缺少 Web access_token，不能上传")
        # Deliberately do not send password, mailbox credentials, RT, cookies or
        # global configuration. chatgpt2api accepts access_token-only imports.
        data = self._request("POST", {"accounts": [{"access_token": token, "email": account.email}]})
        if not all(isinstance(data.get(key), int) and not isinstance(data[key], bool) and data[key] >= 0 for key in ("added", "skipped", "refreshed")):
            raise PoolError("远端上传结果格式不兼容，保留本地账号待确认")
        if not isinstance(data.get("errors"), list):
            raise PoolError("远端上传结果缺少 errors，保留本地账号待确认")
        items = parse_items(data)
        matched = self._match_item(items, account, token)
        if matched is None:
            raise PoolError("上传后远端未确认该 access_token，保留本地账号待重试")
        return self._confirmed_item(matched, len(data["errors"]))


def remember_snapshot(settings: WebPoolSettings, snapshot: dict) -> None:
    config_store.set_many({PREFIX + "last_snapshot": json.dumps({"target_id": settings.target_id, **snapshot}, ensure_ascii=False)})


def last_snapshot(settings: WebPoolSettings) -> dict | None:
    try:
        value = json.loads(config_store.get(PREFIX + "last_snapshot", "{}"))
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("target_id") != settings.target_id:
        return None
    return {key: value[key] for key in ("total_accounts", "normal_accounts", "available_accounts", "remaining_quota", "limited_accounts", "abnormal_accounts", "disabled_accounts", "checked_at") if key in value}


def check_quota(settings: WebPoolSettings) -> dict:
    snapshot = WebPoolClient(settings).snapshot()
    remember_snapshot(settings, snapshot)
    return snapshot


def target_reached(settings: WebPoolSettings, snapshot: dict) -> bool:
    key = "remaining_quota" if settings.target_mode == "quota" else "available_accounts"
    return snapshot[key] >= settings.target_value


def _pending_accounts(settings: WebPoolSettings) -> list[AccountModel]:
    with Session(engine) as session:
        rows = session.exec(select(AccountModel).where(AccountModel.platform == "chatgpt").order_by(AccountModel.id)).all()
    return [row for row in rows if (
        is_web_pool_account(row) and row.status in {"registered", "trial", "subscribed"}
        and row.get_extra().get("chatgpt_web_pool_target") == settings.target_id
        and not (row.get_extra().get("sync_statuses", {}).get("chatgpt2api", {}).get("imported"))
        and (row.token or row.get_extra().get("access_token") or row.get_extra().get("accessToken"))
    )]


def pending_count(settings: WebPoolSettings) -> int:
    return len(_pending_accounts(settings))


def _record_upload(account_id: int, settings: WebPoolSettings, result: dict) -> None:
    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is None:
            raise PoolError("本地账号已删除，不能保存同步结果")
        extra = row.get_extra()
        extra.setdefault("sync_statuses", {})["chatgpt2api"] = {
            **result, "target_id": settings.target_id, "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        row.set_extra(extra)
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()


def upload_account(account_id: int, settings: WebPoolSettings, client: WebPoolClient) -> dict:
    with get_account_lock(account_id):
        with Session(engine) as session:
            row = session.get(AccountModel, account_id)
        if row is None or not is_web_pool_account(row) or row.get_extra().get("chatgpt_web_pool_target") != settings.target_id:
            raise PoolError("账号用途或目标不匹配，拒绝上传")
        try:
            previous = row.get_extra().get("sync_statuses", {}).get("chatgpt2api", {})
            result = client.find_imported(row) if previous.get("uncertain") else None
            if result is None:
                result = client.upload(row)
        except PoolError as exc:
            _record_upload(account_id, settings, {"imported": False, "ready": False, "uncertain": True, "message": str(exc)})
            raise
        # Save completed side effects before responding to stop requests.
        _record_upload(account_id, settings, result)
        return result


@dataclass
class PoolRunResult:
    attempts: int = 0
    registrations: int = 0
    success: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    reason: str = ""
    snapshot: dict | None = None


def replenish(settings: WebPoolSettings, *, control, log: Callable, register: Callable, progress: Callable, should_continue: Callable) -> PoolRunResult:
    client = WebPoolClient(settings)
    result = PoolRunResult()
    failures = 0
    attempted: set[int] = set()
    pause = threading.Event()

    def measure():
        control.checkpoint()
        result.snapshot = client.snapshot()
        remember_snapshot(settings, result.snapshot)
        progress(result)
        log(f"[Web 号池] 剩余图片额度 {result.snapshot['remaining_quota']}，可用账号 {result.snapshot['available_accounts']}")

    def delay():
        deadline = time.monotonic() + settings.delay_seconds
        while time.monotonic() < deadline:
            control.checkpoint()
            pause.wait(min(0.25, max(0, deadline - time.monotonic())))

    measure()  # Failure is unknown, never zero. No registration on an invalid read.
    while result.attempts < settings.max_attempts_per_run:
        control.checkpoint()
        if result.attempts:
            measure()  # Recheck after delay; other writers may have filled the pool.
        if not should_continue():
            result.reason = "自动巡检已关闭或配置已变更，不再注册/上传"
            break
        if target_reached(settings, result.snapshot):
            result.reason = "远端号池已达到目标"
            break
        if failures >= settings.max_consecutive_failures:
            result.reason = "连续失败达到上限，停止本轮"
            break
        candidate = next((row for row in _pending_accounts(settings) if row.id not in attempted), None)
        result.attempts += 1
        progress(result)
        attempt_id = None
        try:
            if candidate is None:
                if not reserve_registration(settings):
                    result.attempts -= 1
                    result.reason = "达到每日注册尝试上限（UTC）"
                    break
                result.registrations += 1
                progress(result)
                log("[Web 注册] 邮箱注册，仅取 Web AT；禁止手机接码、Codex OAuth 和全局自动上传")
                account_id = register(settings, control)
                control.checkpoint()
                if account_id is None:
                    failures += 1
                    result.errors.append("本次 Web 注册未取得 access_token")
                    if result.attempts < settings.max_attempts_per_run and failures < settings.max_consecutive_failures:
                        delay()
                    continue
            else:
                account_id = int(candidate.id)
                log(f"[Web 补传] 优先上传本地待同步账号 #{account_id}，不重复注册")
            attempted.add(account_id)
            control.checkpoint()
            if not should_continue():
                result.reason = "配置已关闭或变更，账号已留在本地待上传"
                break
            # Re-read immediately before sending credentials: a separate registrar
            # may already have filled the target during our registration.
            measure()
            if target_reached(settings, result.snapshot):
                result.reason = "远端已达标，新账号保留本地待下次补传"
                break
            attempt_id = control.start_attempt()
            control.checkpoint(attempt_id=attempt_id)
            uploaded = upload_account(account_id, settings, client)
            if uploaded["ready"]:
                result.success += 1
                failures = 0
                log(f"[Web 上传] 账号 #{account_id} 已确认正常，图片额度 {uploaded['quota']}")
            else:
                failures += 1
                result.errors.append(f"账号 #{account_id} 已入池但未确认可用（{uploaded['status']}，额度 {uploaded['quota']}）")
                log("[Web 上传] 已入池但未确认可用，不计为补号成功")
            measure()  # Verify actual pool, not estimated per-account quota.
        except StopTaskRequested:
            raise
        except SkipCurrentAttemptRequested:
            result.skipped += 1
            failures += 1
            log("[SKIP] 跳过本次操作；已注册账号仍保留本地")
        except PoolError as exc:
            result.errors.append(str(exc))
            result.reason = "远端查询/上传未确认，停止本轮以免重复注册"
            break
        finally:
            if attempt_id is not None:
                control.finish_attempt(attempt_id)
            progress(result)
        if target_reached(settings, result.snapshot):
            result.reason = "远端号池已达到目标"
            break
        if result.attempts < settings.max_attempts_per_run and failures < settings.max_consecutive_failures:
            delay()
    result.reason = result.reason or "达到每轮尝试上限，剩余缺口留待下轮"
    progress(result)
    return result
