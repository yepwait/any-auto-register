"""Account provenance and explicit Web credential synchronization state."""
import hashlib
from datetime import datetime, timezone
from typing import Any

WEB_POOL_PURPOSE = "chatgpt_web_pool"


def is_web_pool_account(account: Any) -> bool:
    if isinstance(account, dict):
        extra = account
    elif hasattr(account, "get_extra"):
        extra = account.get_extra()
    else:
        extra = getattr(account, "extra", {})
    if not isinstance(extra, dict):
        return False
    return extra.get("account_purpose") == WEB_POOL_PURPOSE or str(
        extra.get("chatgpt_web_only", "")
    ).lower() in {"1", "true"}


def web_access_token(extra: dict, token: str = "") -> str:
    return str(extra.get("access_token") or extra.get("accessToken") or token or "").strip()


def credential_fingerprint(access_token: str) -> str:
    """Store a one-way identity, never copy credentials into sync metadata."""
    value = str(access_token or "").strip()
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


def mark_web_credentials_changed(extra: dict, previous_token: str, current_token: str) -> bool:
    if not is_web_pool_account(extra) or not current_token or previous_token == current_token:
        return False
    syncs = extra.get("sync_statuses")
    if not isinstance(syncs, dict):
        return False
    state = syncs.get("chatgpt2api")
    if not isinstance(state, dict) or not state.get("imported"):
        return False
    if state.get("credential_fingerprint") == credential_fingerprint(current_token):
        return False
    state.update(needs_resync=True, ready=False,
                 credentials_changed_at=datetime.now(timezone.utc).isoformat())
    return True
