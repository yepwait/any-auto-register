"""ChatGPT 账号状态判定辅助逻辑。"""

from __future__ import annotations

from typing import Any


INVALID_ACCOUNT_STATUS = "invalid"
REGISTERED_ACCOUNT_STATUS = "registered"
# 账号被 OpenAI 删除/停用时单独成档：和"凭证过期"这类可恢复的失效分开，
# 免得运营再拿这些号去补 RT、重登、刷 CPA。
DELETED_ACCOUNT_STATUS = "deleted"

# 判定为"已删除/已停用"的结论值。只有明确读到 OpenAI 的删除/停用措辞才算，
# 401、relogin_required、泛泛的 403 仍按普通失效处理。
_DELETED_REASONS = frozenset({"auth_deactivated", "codex_deactivated", "remote_deactivated"})


def _lower_text(value: Any) -> str:
    return str(value or "").strip().lower()


def is_account_deactivated_message(error_code: Any = "", message: Any = "") -> bool:
    code = _lower_text(error_code)
    text = _lower_text(message)
    if code in {"account_deactivated", "account_deleted"}:
        return True
    markers = (
        "deleted or deactivated",
        "account has been deleted or deactivated",
        "you do not have an account because it has been deleted or deactivated",
    )
    return any(marker in text for marker in markers)


# 401/403 的响应体里 OpenAI 说封号的措辞不止一种，全部小写后按子串匹配。
# 比 is_account_deactivated_message 宽：那个只认"已删除/已停用"，这里连
# 违规、滥用、封禁一起认，用在"凭证被吊销到底是过期还是封号"这种场合。
_BANNED_BODY_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",
    "potential abuse",
    "terminated",
)


def looks_like_banned_response(body_text: Any) -> bool:
    """凭证被拒的响应体读起来像不像封号。"""
    text = _lower_text(body_text)
    return any(marker in text for marker in _BANNED_BODY_MARKERS)


PLUS_STATUS_UNCHECKED = "unchecked"


def account_plus_status(account: Any) -> str:
    """账号上记录的 Plus 试用结论，没查过算 unchecked。"""
    extra: Any = {}
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        extra = getter() or {}
    else:
        extra = getattr(account, "extra", {}) or {}
    check = extra.get("plus_check") if isinstance(extra, dict) else None
    if not isinstance(check, dict):
        return PLUS_STATUS_UNCHECKED
    return _lower_text(check.get("status")) or PLUS_STATUS_UNCHECKED


def filter_accounts_by_plus_status(accounts: Any, plus_status: Any) -> list:
    wanted = _lower_text(plus_status)
    if not wanted:
        return list(accounts)
    return [account for account in accounts if account_plus_status(account) == wanted]


def account_sms_verified(account: Any) -> bool:
    """Return whether the account has a confirmed SMS verification record."""
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        extra = getter() or {}
    else:
        extra = getattr(account, "extra", {}) or {}
    verification = extra.get("sms_verification") if isinstance(extra, dict) else None
    if not isinstance(verification, dict):
        return False
    value = verification.get("verified")
    if isinstance(value, bool):
        return value
    return _lower_text(value) in {"1", "true", "yes", "y", "verified"}


def filter_accounts_by_sms_verified(accounts: Any, sms_verified: Any) -> list:
    """Filter accounts by whether a confirmed SMS verification exists."""
    wanted = _lower_text(sms_verified)
    if wanted not in {"true", "false"}:
        return list(accounts)
    expected = wanted == "true"
    return [account for account in accounts if account_sms_verified(account) == expected]


def classify_local_probe_state(probe: dict[str, Any] | None) -> str:
    if not isinstance(probe, dict):
        return ""

    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    codex = probe.get("codex") if isinstance(probe.get("codex"), dict) else {}

    auth_state = _lower_text(auth.get("state"))
    auth_status = int(auth.get("http_status") or 0)
    auth_error_code = auth.get("error_code")
    auth_message = auth.get("message")

    if auth_status == 401 or auth_state in {"access_token_invalidated", "unauthorized"}:
        return "auth_401"
    if is_account_deactivated_message(auth_error_code, auth_message):
        return "auth_deactivated"
    if auth_status == 403 and auth_state in {"account_deactivated", "banned_like"}:
        return "auth_403"

    codex_state = _lower_text(codex.get("state"))
    codex_status = int(codex.get("http_status") or 0)
    codex_error_code = codex.get("error_code")
    codex_message = codex.get("message")

    if codex_status == 401 or codex_state in {"access_token_invalidated", "unauthorized"}:
        return "codex_401"
    if is_account_deactivated_message(codex_error_code, codex_message):
        return "codex_deactivated"
    if codex_status == 403 and codex_state == "account_deactivated":
        return "codex_403"

    return ""


def classify_remote_sync_state(sync: dict[str, Any] | None) -> str:
    if not isinstance(sync, dict):
        return ""

    remote_state = _lower_text(sync.get("remote_state"))
    status_code = int(sync.get("last_probe_status_code") or 0)
    error_code = sync.get("last_probe_error_code")
    message = sync.get("last_probe_message") or sync.get("status_message") or sync.get("message")

    if status_code == 401 or remote_state in {"access_token_invalidated", "unauthorized"}:
        return "remote_401"
    if remote_state == "relogin_required":
        return "remote_relogin_required"
    if is_account_deactivated_message(error_code, message):
        return "remote_deactivated"
    if status_code == 403 and remote_state in {"account_deactivated", "banned_like"}:
        return "remote_403"

    return ""


def apply_chatgpt_status_policy(
    account: Any,
    *,
    local_probe: dict[str, Any] | None = None,
    remote_sync: dict[str, Any] | None = None,
) -> str:
    reason = classify_local_probe_state(local_probe) or classify_remote_sync_state(remote_sync)
    if reason:
        # 备注：删除/停用单独记 deleted，其余（401、relogin_required、泛 403）统一 invalid。
        status = DELETED_ACCOUNT_STATUS if reason in _DELETED_REASONS else INVALID_ACCOUNT_STATUS
        setattr(account, "status", status)
        return reason

    local_auth = local_probe.get("auth") if isinstance(local_probe, dict) else {}
    local_auth = local_auth if isinstance(local_auth, dict) else {}
    local_valid = (
        _lower_text(local_auth.get("state")) == "access_token_valid"
        and int(local_auth.get("http_status") or 0) == 200
    )
    remote_valid = (
        isinstance(remote_sync, dict)
        and _lower_text(remote_sync.get("remote_state")) == "usable"
        and int(remote_sync.get("last_probe_status_code") or 0) == 200
    )
    # 备注：只有明确的 200 成功结果才洗掉历史 invalid/deleted；超时、未探测等状态一律不猜。
    if _lower_text(getattr(account, "status", "")) in {
        INVALID_ACCOUNT_STATUS,
        DELETED_ACCOUNT_STATUS,
    }:
        if local_valid:
            setattr(account, "status", REGISTERED_ACCOUNT_STATUS)
            return "local_valid"
        if remote_valid:
            setattr(account, "status", REGISTERED_ACCOUNT_STATUS)
            return "remote_valid"
    return ""


# ── RT 账号就绪判定 ──
# 库存与补库的统一口径：账号主状态正常、持有 refresh_token、且没有被明确判定为
# 失效／停用。短信验证是独立事实，不再是“可用 RT 账号”的必要条件。
NORMAL_ACCOUNT_STATUSES = frozenset({"registered", "trial", "subscribed"})

# 这些分类代表凭证已明确不可用，既不计入库存，也不再作为日常恢复候选。
_UNUSABLE_REASONS = frozenset(
    {"auth_401", "auth_deactivated", "auth_403", "codex_401", "codex_deactivated", "codex_403"}
)


def _account_extra(account: Any) -> dict:
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        try:
            extra = getter() or {}
        except Exception:
            extra = {}
    else:
        extra = getattr(account, "extra", {}) or {}
    return extra if isinstance(extra, dict) else {}


def account_refresh_token(account: Any) -> str:
    """读取账号真正持有的 RT，兼容历史 camelCase 写法。"""
    extra = _account_extra(account)
    return str(extra.get("refresh_token") or extra.get("refreshToken") or "").strip()


def account_probe_state(account: Any) -> str:
    """把本地测活快照归成可判断的粗粒度状态。"""
    probe = _account_extra(account).get("chatgpt_local")
    probe = probe if isinstance(probe, dict) else {}
    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    codex = probe.get("codex") if isinstance(probe.get("codex"), dict) else {}
    auth_state = _lower_text(auth.get("state"))
    codex_state = _lower_text(codex.get("state"))
    reason = classify_local_probe_state(probe)
    if reason in {"auth_deactivated", "codex_deactivated"}:
        return "deactivated"
    if reason in {"auth_401", "auth_403", "codex_401", "codex_403"}:
        return "invalid"
    if auth_state == "access_token_valid":
        if codex_state == "usable":
            return "usable"
        if codex_state in {"payment_required", "quota_exhausted"}:
            return "restricted"
        return "auth_ok"
    if auth_state == "account_deactivated":
        return "deactivated"
    if auth_state in {"unauthorized", "access_token_invalidated", "missing_access_token"}:
        return "invalid"
    return ""


def account_rt_ready(account: Any) -> bool:
    """是否属于“可用 RT 账号”：正常状态 + 有 RT + 无明确不可用证据。"""
    if _lower_text(getattr(account, "status", "")) not in NORMAL_ACCOUNT_STATUSES:
        return False
    if not account_refresh_token(account):
        return False
    return classify_local_probe_state(_account_extra(account).get("chatgpt_local")) not in _UNUSABLE_REASONS


def account_rt_recovery_pending(account: Any) -> bool:
    """是否值得作为补 RT 候选：尚未就绪、但仍有可尝试的凭证或密码。"""
    status = _lower_text(getattr(account, "status", ""))
    if status in {"deleted", "banned", "expired"}:
        return False
    if account_rt_ready(account):
        return False
    reason = classify_local_probe_state(_account_extra(account).get("chatgpt_local"))
    # 明确停用/封禁的号不再自动恢复，避免把死号反复拖进队列。
    if reason in {"auth_deactivated", "codex_deactivated", "auth_403", "codex_403"}:
        return False
    extra = _account_extra(account)
    return bool(
        account_refresh_token(account)
        or extra.get("access_token")
        or extra.get("session_token")
        or getattr(account, "token", "")
        or getattr(account, "password", "")
    )
