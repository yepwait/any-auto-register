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
from urllib.parse import urlsplit, quote

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, engine
from core.task_runtime import SkipCurrentAttemptRequested, StopTaskRequested
from services.account_locks import get_account_lock
from services.chatgpt_account_purpose import is_web_pool_account, web_access_token, credential_fingerprint

SOURCE = "chatgpt_web_pool"
REGISTER_SOURCE = "chatgpt_web_pool_register"
PREFIX = "chatgpt_web_pool_"
_budget_lock = threading.Lock()


class PoolError(ValueError):
    """Messages must be safe for logs: no response bodies, tokens or URLs."""


class QuotaUnavailable(PoolError):
    """A valid remote response cannot be compared with a numeric quota goal."""


class WebPoolSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = ""
    admin_token: str = Field(default="", repr=False)
    target_mode: Literal["quota", "available_accounts"] = "quota"
    target_value: int = Field(default=100, ge=1, le=1000000)
    refill_below: int = Field(default=0, ge=0, le=1000000)  # 0 uses target_value (legacy behavior)
    interval_minutes: int = Field(default=30, ge=1, le=10080)
    max_attempts_per_run: int = Field(default=5, ge=0, le=100)  # 0 = no per-run count cap
    daily_registration_limit: int = Field(default=20, ge=0, le=1000)  # 0 = no daily cap
    max_consecutive_failures: int = Field(default=3, ge=0, le=10)  # 0 = no consecutive-failure cap
    delay_seconds: float = Field(default=5, ge=0, le=3600, allow_inf_nan=False)
    request_timeout: int = Field(default=60, ge=5, le=180)
    protocol: Literal["auto", "legacy", "yukkcat_v3"] = "auto"

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.refill_below > self.target_value:
            raise ValueError("开始补号阈值不能高于补足目标；0 表示与目标相同")
        return self

    @property
    def start_threshold(self) -> int:
        return self.refill_below or self.target_value

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


def parse_v3_items(data) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise PoolError("远端 v3 账号列表格式不兼容")
    result = []
    for item in data["items"]:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            raise PoolError("远端 v3 账号缺少稳定 ID")
        status = item.get("backend_status")
        if status not in {"正常", "限流", "异常", "禁用"}:
            raise PoolError("远端 v3 账号状态格式不兼容")
        for key in ("available", "quota_unknown", "quota_unlimited"):
            if not isinstance(item.get(key), bool):
                raise PoolError("远端 v3 额度语义不兼容，未执行注册")
        result.append({
            "id": item["id"], "email": str(item.get("email") or ""),
            "user_id": str(item.get("user_id") or ""), "status": status,
            "quota": _quota(item.get("quota_remaining")), "available": item["available"],
            "image_quota_unknown": item["quota_unknown"], "quota_unlimited": item["quota_unlimited"],
        })
    return result


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
        self._protocol = settings.protocol
        self.checkpoint: Callable[[], None] = lambda: None
        self._operation_deadline: float | None = None

    def _request(self, method: str, payload=None, *, path: str = "/api/accounts") -> dict:
        # Never follow redirects with account credentials. Ignore process-level
        # proxy/netrc: the explicitly configured management server is the target.
        self.checkpoint()
        timeout = float(self.settings.request_timeout)
        if self._operation_deadline is not None:
            remaining = self._operation_deadline - time.monotonic()
            if remaining <= 0:
                raise PoolError("远端操作超过本轮超时，保留本地账号待复核")
            timeout = min(timeout, remaining)
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(
                    method, self.settings.base_url + path,
                    headers={"Authorization": f"Bearer {self.settings.admin_token}", "Accept": "application/json"},
                    json=payload, timeout=timeout, allow_redirects=False,
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

    def _list_accounts(self) -> list[dict]:
        data = self._request("GET")
        items = data.get("items") if isinstance(data, dict) else None
        looks_v3 = isinstance(data, dict) and ("page_size" in data or (
            isinstance(items, list) and bool(items) and isinstance(items[0], dict) and "quota_remaining" in items[0]))
        if self._protocol == "auto":
            self._protocol = "yukkcat_v3" if looks_v3 else "legacy"
        if self._protocol == "legacy":
            if looks_v3:
                raise PoolError("远端是 yukkcat v3，请切换协议或使用自动识别")
            return parse_items(data)
        combined = parse_v3_items(data)
        for key in ("total", "page", "page_size"):
            if isinstance(data.get(key), bool) or not isinstance(data.get(key), int):
                raise PoolError("远端 v3 分页信息不完整，停止补号")
        total, size, page = data["total"], data["page_size"], data["page"]
        all_total = data.get("all_total", total)
        if (total < 0 or total > 100000 or not 1 <= size <= 500 or page != 1 or
                isinstance(all_total, bool) or not isinstance(all_total, int) or all_total != total or
                len(combined) > size):
            raise PoolError("远端 v3 分页范围无效或不是完整号池，停止补号")
        while len(combined) < total:
            page += 1
            if page > 10000:
                raise PoolError("远端分页过多，停止检查")
            following = self._request("GET", path=f"/api/accounts?page={page}&page_size={size}")
            if (following.get("total") != total or following.get("page") != page or
                    following.get("page_size") != size or following.get("all_total", total) != all_total):
                raise PoolError("检查期间远端号池或分页范围已变化，请稍后重试")
            more = parse_v3_items(following)
            if not more or len(more) > size:
                raise PoolError("远端 v3 分页提前结束，不能当作空号池")
            combined.extend(more)
        ids = [item["id"] for item in combined]
        if len(combined) != total or len(set(ids)) != total:
            raise PoolError("远端 v3 分页重复或不完整，停止补号")
        return combined

    def snapshot(self) -> dict:
        items = self._list_accounts()
        if self._protocol == "legacy":
            result = summarize(items)
            result.update(protocol="legacy", quota_known=True, unknown_quota_accounts=0, unlimited_accounts=0)
            return result
        available = [item for item in items if item["available"] and item["status"] == "正常"]
        unknown = sum(item["image_quota_unknown"] and not item["quota_unlimited"] for item in available)
        unlimited = sum(item["quota_unlimited"] for item in available)
        # A numeric target is not comparable with unknown/unlimited accounts;
        # expose those states instead of inventing 0 or infinity and spending.
        quota_known = not (unknown or unlimited)
        return {
            "total_accounts": len(items), "normal_accounts": sum(item["status"] == "正常" for item in items),
            "available_accounts": len(available),
            "remaining_quota": sum(item["quota"] for item in available) if quota_known else None,
            "known_remaining_quota": sum(item["quota"] for item in available if not item["image_quota_unknown"] and not item["quota_unlimited"]),
            "quota_known": quota_known, "unknown_quota_accounts": unknown, "unlimited_accounts": unlimited,
            "limited_accounts": sum(item["status"] == "限流" for item in items),
            "abnormal_accounts": sum(item["status"] == "异常" for item in items),
            "disabled_accounts": sum(item["status"] == "禁用" for item in items),
            "checked_at": datetime.now(timezone.utc).isoformat(), "protocol": "yukkcat_v3",
        }

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
        if self._protocol == "yukkcat_v3":
            # v3 deliberately hides tokens; an ID/email alone does not prove that
            # new credentials arrived. Reapply by stable ID and sync idempotently.
            return None
        extra = account.get_extra()
        token = str(extra.get("access_token") or extra.get("accessToken") or account.token or "").strip()
        items = parse_items(self._request("GET"))
        matched = self._match_item(items, account, token)
        return self._confirmed_item(matched) if matched is not None else None

    def _v3_confirmation(self, items: list[dict], remote_id: str, errors: list) -> dict:
        matched = next((item for item in items if item["id"] == remote_id), None)
        if matched is None:
            raise PoolError("远端 v3 未确认目标账号，保留本地待重试")
        ready = matched["available"] and matched["status"] == "正常" and not errors
        return {"imported": True, "ready": ready, "status": matched["status"],
                "quota": matched["quota"] if not matched["image_quota_unknown"] else None,
                "quota_unknown": matched["image_quota_unknown"], "quota_unlimited": matched["quota_unlimited"],
                "remote_id": remote_id, "protocol": "yukkcat_v3", "refresh_errors": len(errors)}

    @staticmethod
    def _validate_v3_mutation(data: dict, count_keys: tuple[str, ...]) -> None:
        for key in count_keys:
            if type(data.get(key)) is not int or data[key] < 0:
                raise PoolError("远端 v3 导入/更新计数字段不兼容")
        for key in ("updated_ids", "removed_ids", "errors", "events", "items"):
            if not isinstance(data.get(key), list):
                raise PoolError("远端 v3 导入/更新结果格式不兼容")
        for key in ("updated_ids", "removed_ids"):
            if any(not isinstance(value, str) or not value.strip() for value in data[key]):
                raise PoolError("远端 v3 返回无效账号 ID")

    def _upload_v3(self, account: AccountModel, token: str) -> dict:
        state = (account.get_extra().get("sync_statuses") or {}).get("chatgpt2api") or {}
        remote_id = str(state.get("remote_id") or "").strip()
        if state.get("imported") and not remote_id:
            raise PoolError("旧同步记录没有远端 ID，无法安全更新 v3 凭证；请先核对远端账号映射")
        if remote_id:
            # Stable management ID prevents AT rotation from creating another row.
            data = self._request("POST", {"id": remote_id, "access_token": token}, path="/api/accounts/update")
            self._validate_v3_mutation(data, ("updated", "removed"))
            if data["updated_ids"] != [remote_id] or data["removed_ids"] or data["errors"] or data["updated"] != 1:
                raise PoolError("远端 v3 凭证更新未确认或账号已移除")
            # /update only persists credentials; follow with a sync of this ID.
            operation = self._request("POST", {"account_ids": [remote_id]}, path="/api/accounts/sync")
            progress_id = str(operation.get("progress_id") or "").strip()
            if (not progress_id or operation.get("target_ids") != [remote_id] or
                    not isinstance(operation.get("errors"), list) or operation["errors"]):
                raise PoolError("远端 v3 同步未确认目标或进度 ID，保留待复核")
            deadline = time.monotonic() + self.settings.request_timeout
            self._operation_deadline = deadline
            while time.monotonic() < deadline:
                self.checkpoint()
                progress = self._request("GET", path="/api/accounts/operations/" + quote(progress_id, safe=""))
                if type(progress.get("done")) is not bool:
                    raise PoolError("远端 v3 同步进度缺少有效完成标记")
                if progress.get("error") or str(progress.get("stage") or "").lower() in {"failed", "error", "cancelled"}:
                    raise PoolError("远端 v3 同步失败，保留待复核")
                if progress["done"]:
                    result = progress.get("result")
                    if not isinstance(result, dict):
                        raise PoolError("远端 v3 同步完成但结果缺失")
                    # Completion uses done, never a guessed status string. A
                    # receipt for another account is not confirmation of ours.
                    self._validate_v3_mutation(result, ())
                    if result["updated_ids"] != [remote_id] or result["removed_ids"] or result["errors"]:
                        raise PoolError("远端 v3 同步后目标未确认、已移除或失败")
                    detail = self._request("GET", path="/api/accounts/" + quote(remote_id, safe=""))
                    item = detail.get("item")
                    if not isinstance(item, dict):
                        raise PoolError("远端 v3 账号详情缺失")
                    return self._v3_confirmation(parse_v3_items({"items": [item]}), remote_id, [])
                time.sleep(0.5)
            raise PoolError("远端 v3 同步超时，账号已留在本地待复核")
        data = self._request("POST", {"accounts": [{"access_token": token, "email": account.email}],
                                      "sync_after_import": True, "return_items": True})
        self._validate_v3_mutation(data, ("added", "skipped", "synced"))
        ids, removed, errors = data.get("updated_ids"), data.get("removed_ids"), data.get("errors")
        if (not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or
                not isinstance(removed, list) or not isinstance(errors, list)):
            raise PoolError("远端 v3 导入返回格式不兼容，保留本地账号待复核")
        if ids[0] in removed:
            raise PoolError("远端 v3 导入账号已被移除，未计为成功")
        return self._v3_confirmation(parse_v3_items(data), ids[0], errors)

    def upload(self, account: AccountModel) -> dict:
        extra = account.get_extra()
        token = str(extra.get("access_token") or extra.get("accessToken") or account.token or "").strip()
        if not token:
            raise PoolError("本地账号缺少 Web access_token，不能上传")
        if self._protocol == "yukkcat_v3":
            try:
                return self._upload_v3(account, token)
            finally:
                self._operation_deadline = None
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
    return {key: value[key] for key in ("total_accounts", "normal_accounts", "available_accounts", "remaining_quota", "known_remaining_quota", "quota_known", "unknown_quota_accounts", "unlimited_accounts", "protocol", "limited_accounts", "abnormal_accounts", "disabled_accounts", "checked_at") if key in value}


def check_quota(settings: WebPoolSettings) -> dict:
    snapshot = WebPoolClient(settings).snapshot()
    remember_snapshot(settings, snapshot)
    return snapshot


def _target_measure(settings: WebPoolSettings, snapshot: dict) -> int:
    key = "remaining_quota" if settings.target_mode == "quota" else "available_accounts"
    value = snapshot.get(key)
    if value is None or (settings.target_mode == "quota" and snapshot.get("quota_known") is False):
        raise QuotaUnavailable("远端存在未知/无限图片额度，无法比较数值目标；请核对额度或改用可用账号数模式")
    return _quota(value)


def target_reached(settings: WebPoolSettings, snapshot: dict) -> bool:
    return _target_measure(settings, snapshot) >= settings.target_value


def should_start_refill(settings: WebPoolSettings, snapshot: dict) -> bool:
    return _target_measure(settings, snapshot) < settings.start_threshold


def _pending_accounts(settings: WebPoolSettings) -> list[AccountModel]:
    with Session(engine) as session:
        rows = session.exec(select(AccountModel).where(AccountModel.platform == "chatgpt")
                            .where(AccountModel.status.in_(["registered", "trial", "subscribed"]))
                            .order_by(AccountModel.id)).all()
    pending = []
    for row in rows:
        extra = row.get_extra()
        token = web_access_token(extra, row.token)
        if not token or not is_web_pool_account(extra) or extra.get("chatgpt_web_pool_target") != settings.target_id:
            continue
        syncs = extra.get("sync_statuses") or {}
        state = syncs.get("chatgpt2api") or {}
        fingerprint = state.get("credential_fingerprint")
        if (not state.get("imported") or state.get("needs_resync") or
                (fingerprint and fingerprint != credential_fingerprint(token))):
            pending.append(row)
    return pending


def pending_count(settings: WebPoolSettings) -> int:
    return len(_pending_accounts(settings))


def _record_upload(account_id: int, settings: WebPoolSettings, result: dict, *, sent_fingerprint: str = "") -> None:
    with Session(engine) as session:
        row = session.get(AccountModel, account_id)
        if row is None:
            raise PoolError("本地账号已删除，不能保存同步结果")
        extra = row.get_extra()
        previous = extra.setdefault("sync_statuses", {}).get("chatgpt2api") or {}
        if result.get("imported"):
            current_fingerprint = credential_fingerprint(web_access_token(extra, row.token))
            confirmed_fingerprint = sent_fingerprint or current_fingerprint
            changed_during_upload = confirmed_fingerprint != current_fingerprint
            state = {**previous, **result,
                     "credential_fingerprint": confirmed_fingerprint,
                     "needs_resync": changed_during_upload, "uncertain": False}
            if changed_during_upload:
                state["ready"] = False
        else:
            # Keep prior confirmed remote identity for reconciliation; failed
            # resync must not lose the fact that the old credential was imported.
            state = {**previous, **result, "imported": bool(previous.get("imported")),
                     "needs_resync": bool(previous.get("imported")) or bool(previous.get("needs_resync")),
                     "ready": False}
        extra["sync_statuses"]["chatgpt2api"] = {
            **state, "target_id": settings.target_id, "checked_at": datetime.now(timezone.utc).isoformat(),
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
        sent_fingerprint = credential_fingerprint(web_access_token(row.get_extra(), row.token))
        try:
            previous = row.get_extra().get("sync_statuses", {}).get("chatgpt2api", {})
            result = client.find_imported(row) if previous.get("uncertain") else None
            if result is None:
                result = client.upload(row)
        except (StopTaskRequested, SkipCurrentAttemptRequested):
            # Remote update/import may already have happened; keep a safe retry
            # marker without claiming it was confirmed.
            _record_upload(account_id, settings, {"imported": False, "ready": False, "uncertain": True,
                                                  "message": "同步被中断，保留待复核"})
            raise
        except PoolError as exc:
            _record_upload(account_id, settings, {"imported": False, "ready": False, "uncertain": True, "message": str(exc)})
            raise
        # Save completed side effects before responding to stop requests.
        _record_upload(account_id, settings, result, sent_fingerprint=sent_fingerprint)
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
    refill_started: bool = False
    budget_exhausted: bool = False
    retryable_failure: bool = False
    error_count: int = 0

    def add_error(self, message: str) -> None:
        # An unlimited run must not retain an unlimited in-memory error array.
        self.error_count += 1
        self.errors.append(message)
        if len(self.errors) > 100:
            del self.errors[:-100]


def replenish(settings: WebPoolSettings, *, control, log: Callable, register: Callable, progress: Callable, should_continue: Callable, refill_active: bool = False) -> PoolRunResult:
    client = WebPoolClient(settings)
    result = PoolRunResult()
    upload_attempt_id: int | None = None
    def request_checkpoint():
        control.checkpoint(consume_skip=upload_attempt_id is not None, attempt_id=upload_attempt_id)
        if not should_continue():
            raise StopTaskRequested("配置已关闭或改变，停止后续远端请求")
    client.checkpoint = request_checkpoint
    failures = 0
    attempted: set[int] = set()
    pause = threading.Event()

    def measure():
        control.checkpoint(consume_skip=False)
        result.snapshot = client.snapshot()
        remember_snapshot(settings, result.snapshot)
        progress(result)
        quota = result.snapshot['remaining_quota']
        quota_label = '未知' if quota is None else str(quota)
        log(f"[Web 号池] 剩余图片额度 {quota_label}，可用账号 {result.snapshot['available_accounts']}")

    def may_attempt() -> bool:
        return settings.max_attempts_per_run == 0 or result.attempts < settings.max_attempts_per_run

    def failure_limit_reached() -> bool:
        return settings.max_consecutive_failures > 0 and failures >= settings.max_consecutive_failures

    def delay():
        # With no count bounds and immediate mock/invalid-provider failures,
        # delay=0 must not produce a tight retry loop. This is pacing, not a cap.
        seconds = settings.delay_seconds
        if settings.max_attempts_per_run == 0:
            seconds = max(seconds, 1.0 if failures else 0.25)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            control.checkpoint(consume_skip=False)
            if not should_continue():
                raise StopTaskRequested("配置已关闭或改变，停止后续尝试")
            pause.wait(min(0.25, max(0, deadline - time.monotonic())))

    measure()  # Failure is unknown, never zero. No registration on an invalid read.
    result.refill_started = refill_active or should_start_refill(settings, result.snapshot)
    def resync_pending(row):
        state = (row.get_extra().get("sync_statuses") or {}).get("chatgpt2api") or {}
        return bool(state.get("imported") and (state.get("needs_resync") or (
            state.get("credential_fingerprint") and state["credential_fingerprint"] != credential_fingerprint(web_access_token(row.get_extra(), row.token)))))

    if not result.refill_started and not any(resync_pending(row) for row in _pending_accounts(settings)):
        result.reason = "远端额度未低于补号阈值，无需注册"
        progress(result)
        return result
    while may_attempt():
        control.checkpoint(consume_skip=False)
        if result.attempts:
            measure()  # Recheck after delay; other writers may have filled the pool.
        if not should_continue():
            result.reason = "自动巡检已关闭或配置已变更，不再注册/上传"
            break
        pending_rows = _pending_accounts(settings)
        # Forget confirmed rows between iterations; an unlimited run should not
        # accumulate every account ID ever uploaded in a process-local set.
        attempted.intersection_update(row.id for row in pending_rows)
        pending = [row for row in pending_rows if row.id not in attempted]
        resync = next((row for row in pending if resync_pending(row)), None)
        if (target_reached(settings, result.snapshot) or not result.refill_started) and resync is None:
            result.reason = "远端号池已达到目标" if result.refill_started else "未低于补号阈值，无需注册"
            break
        if failure_limit_reached():
            result.reason = "连续失败达到上限，停止本轮"
            break
        candidate = resync or next(iter(pending), None)
        result.attempts += 1
        progress(result)
        attempt_id = None
        try:
            if candidate is None:
                if not reserve_registration(settings):
                    result.attempts -= 1
                    result.reason = "达到每日注册尝试上限（UTC）"
                    result.budget_exhausted = True
                    break
                result.registrations += 1
                progress(result)
                log("[Web 注册] 邮箱注册，仅取 Web AT；禁止手机接码、Codex OAuth 和全局自动上传")
                account_id = register(settings, control)
                control.checkpoint()
                if account_id is None:
                    failures += 1
                    result.add_error("本次 Web 注册未取得 access_token")
                    log(f"[FAIL] 第 {result.attempts} 次尝试未取得 Web AT；连续失败 {failures} 次")
                    if may_attempt() and not failure_limit_reached():
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
            if target_reached(settings, result.snapshot) and resync is None:
                result.reason = "远端已达标，新账号保留本地待下次补传"
                break
            attempt_id = control.start_attempt()
            upload_attempt_id = attempt_id
            control.checkpoint(attempt_id=attempt_id)
            uploaded = upload_account(account_id, settings, client)
            if uploaded["ready"]:
                result.success += 1
                failures = 0
                log(f"[Web 上传] 账号 #{account_id} 已确认正常，图片额度 {uploaded['quota']}")
            else:
                failures += 1
                result.add_error(f"账号 #{account_id} 已入池但未确认可用（{uploaded['status']}，额度 {uploaded['quota']}）")
                log("[Web 上传] 已入池但未确认可用，不计为补号成功")
            measure()  # Verify actual pool, not estimated per-account quota.
        except StopTaskRequested:
            raise
        except SkipCurrentAttemptRequested:
            result.skipped += 1
            failures += 1
            log("[SKIP] 跳过本次操作；已注册账号仍保留本地")
        except QuotaUnavailable:
            raise
        except PoolError as exc:
            result.add_error(str(exc))
            result.reason = "远端查询/上传未确认，停止本轮以免重复注册"
            result.retryable_failure = True
            break
        finally:
            upload_attempt_id = None
            if attempt_id is not None:
                control.finish_attempt(attempt_id)
            progress(result)
        if target_reached(settings, result.snapshot) and not any(
            row.id not in attempted and resync_pending(row) for row in _pending_accounts(settings)
        ):
            result.reason = "远端号池已达到目标"
            break
        if may_attempt() and not failure_limit_reached():
            delay()
    result.reason = result.reason or "达到每轮尝试上限，剩余缺口留待下轮"
    progress(result)
    return result
