"""Keep Web-only identities out of Codex/phone inventory automation."""
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
