"""ChatGPT 注册模式适配器。

这里有两个互相独立的维度：

``mode`` 决定要不要 refresh_token —— ``refresh_token`` 模式会额外走一次独立
authorize 链换 refresh_token，``access_token_only`` 模式直接跳过（省约 10 秒
且不产生必然失败的告警）。

``register_flow`` 决定拿什么当注册身份 —— 邮箱、接码平台的手机号，或者手机号
注册完再把邮箱池里的地址绑上去。两个维度可以任意组合。

``bind_2fa`` 是个独立开关：注册成功后顺手给账号绑一个 TOTP 双因素，密钥随号
落库。默认关，因为绑上之后每次登录都要动态码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus
from platforms.chatgpt.registration_engine import (
    REGISTER_FLOW_EMAIL,
    REGISTER_FLOW_PHONE,
    REGISTER_FLOW_PHONE_WITH_EMAIL,
    REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    REGISTRATION_MODE_REFRESH_TOKEN,
    ChatGPTRegistrationEngine,
    RegistrationResult,
    is_chatgpt_web_only,
)

CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN = REGISTRATION_MODE_REFRESH_TOKEN
CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY = REGISTRATION_MODE_ACCESS_TOKEN_ONLY
DEFAULT_CHATGPT_REGISTRATION_MODE = CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN

CHATGPT_REGISTER_FLOW_EMAIL = REGISTER_FLOW_EMAIL
CHATGPT_REGISTER_FLOW_PHONE = REGISTER_FLOW_PHONE
CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL = REGISTER_FLOW_PHONE_WITH_EMAIL
DEFAULT_CHATGPT_REGISTER_FLOW = CHATGPT_REGISTER_FLOW_EMAIL

# 邮箱链路失效的特征串，命中就换下一个邮箱重试而不是把整轮判死
_MAILBOX_ERROR_MARKERS = ("service_abuse_mode", "oauth_token_failed", "imap")
_MAX_ATTEMPTS = 3


def normalize_chatgpt_registration_mode(value) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {
        CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        "access_token",
        "at_only",
        "without_rt",
        "without_refresh_token",
        "no_rt",
        "0",
        "false",
    }:
        return CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
    if normalized in {
        CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
        "rt",
        "with_rt",
        "has_rt",
        "1",
        "true",
    }:
        return CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
    return DEFAULT_CHATGPT_REGISTRATION_MODE


def resolve_chatgpt_registration_mode(extra: Optional[dict]) -> str:
    extra = extra or {}
    if is_chatgpt_web_only(extra):
        return CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
    if "chatgpt_registration_mode" in extra:
        return normalize_chatgpt_registration_mode(extra.get("chatgpt_registration_mode"))
    if "chatgpt_has_refresh_token_solution" in extra:
        return (
            CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
            if bool(extra.get("chatgpt_has_refresh_token_solution"))
            else CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
        )
    return DEFAULT_CHATGPT_REGISTRATION_MODE


def normalize_chatgpt_register_flow(value) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {
        CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL,
        "phone_bind_email",
        "phone_and_email",
        "phone_email",
    }:
        return CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL
    if normalized in {CHATGPT_REGISTER_FLOW_PHONE, "phone_number", "sms"}:
        return CHATGPT_REGISTER_FLOW_PHONE
    return DEFAULT_CHATGPT_REGISTER_FLOW


def resolve_chatgpt_register_flow(extra: Optional[dict]) -> str:
    extra = extra or {}
    if is_chatgpt_web_only(extra):
        return CHATGPT_REGISTER_FLOW_EMAIL
    return normalize_chatgpt_register_flow(extra.get("chatgpt_register_flow"))


def resolve_chatgpt_bind_2fa(extra: Optional[dict]) -> bool:
    """要不要在注册成功后顺手绑 TOTP 2FA。

    默认关：绑上之后这个号的每次登录都要动态码，密钥又只下发一次，丢了就再也
    登不进去。愿意承担这个代价的人自己在任务里勾。
    """
    if is_chatgpt_web_only(extra):
        return False
    value = (extra or {}).get("chatgpt_bind_2fa")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class ChatGPTRegistrationContext:
    mailbox: BaseMailbox
    proxy_url: Optional[str]
    callback_logger: Callable[[str], None]
    email: Optional[str]
    password: Optional[str]
    extra_config: dict
    mailbox_kind: str = "mailbox"


class ChatGPTRegistrationModeAdapter:
    """按模式跑注册引擎，并把结果转成平台层的 ``Account``。"""

    def __init__(
        self,
        mode: str,
        register_flow: str = DEFAULT_CHATGPT_REGISTER_FLOW,
        bind_2fa: bool = False,
        web_only: bool = False,
    ):
        self.web_only = web_only
        self.mode = CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY if web_only else mode
        self.register_flow = CHATGPT_REGISTER_FLOW_EMAIL if web_only else register_flow
        self.bind_2fa = False if web_only else bind_2fa

    def run(self, context: ChatGPTRegistrationContext) -> RegistrationResult:
        if is_chatgpt_web_only(context.extra_config):
            self.web_only = True
            self.mode = CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
            self.register_flow = CHATGPT_REGISTER_FLOW_EMAIL
            self.bind_2fa = False
        extra_config = dict(context.extra_config)
        if self.web_only:
            extra_config["chatgpt_web_only"] = "1"
        # Web 池由外层预算调度补号，内部重试会额外消耗邮箱并超出预算。
        max_attempts = 1 if self.web_only else _MAX_ATTEMPTS
        result: Optional[RegistrationResult] = None
        for attempt in range(max_attempts):
            engine = ChatGPTRegistrationEngine(
                mailbox=context.mailbox,
                mode=self.mode,
                proxy=context.proxy_url,
                email=context.email or "",
                password=context.password or "",
                extra_config=extra_config,
                log_fn=context.callback_logger,
                mailbox_kind=context.mailbox_kind,
                register_flow=self.register_flow,
                bind_2fa=self.bind_2fa,
            )
            result = engine.run()
            if result.success:
                return result

            error = str(result.error_message or "").lower()
            marker = next((m for m in _MAILBOX_ERROR_MARKERS if m in error), None)
            if marker is None or attempt >= max_attempts - 1:
                break
            context.callback_logger(
                f"邮箱链路失效（{marker}），换用下一个邮箱重试 "
                f"({attempt + 1}/{max_attempts - 1})…"
            )
        return result

    def build_account(self, result: RegistrationResult, fallback_password: str) -> Account:
        return Account(
            platform="chatgpt",
            email=result.email,
            password=result.password or fallback_password,
            user_id=result.account_id,
            token=result.access_token,
            status=AccountStatus.REGISTERED,
            extra=self._build_account_extra(result),
        )

    def _build_account_extra(self, result: RegistrationResult) -> dict:
        metadata = dict(result.metadata or {})
        if self.web_only:
            # 这些 metadata 字段来自邮箱池，不能覆盖真正的 ChatGPT 凭据。
            # 旧模式的字段布局保持不变。
            for key in ("refresh_token", "client_id", "password"):
                value = metadata.pop(key, None)
                if value not in (None, "", False):
                    metadata.setdefault(f"outlook_{key}", value)
        return {
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
            "id_token": result.id_token,
            "session_token": result.session_token,
            "cookies": result.cookie_header,
            "workspace_id": result.workspace_id,
            "chatgpt_registration_mode": self.mode,
            "chatgpt_has_refresh_token_solution": self.mode == CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
            "chatgpt_register_flow": self.register_flow,
            "chatgpt_bind_2fa": self.bind_2fa,
            "chatgpt_token_source": result.source,
            **{k: v for k, v in metadata.items() if v not in (None, "", False)},
        }


def build_chatgpt_registration_mode_adapter(
    extra: Optional[dict],
) -> ChatGPTRegistrationModeAdapter:
    return ChatGPTRegistrationModeAdapter(
        resolve_chatgpt_registration_mode(extra),
        resolve_chatgpt_register_flow(extra),
        resolve_chatgpt_bind_2fa(extra),
        web_only=is_chatgpt_web_only(extra),
    )
