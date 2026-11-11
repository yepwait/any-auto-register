"""ChatGPT / Codex CLI 平台插件"""

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, BasePlatform, RegisterConfig
from core.registry import register
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
)
from platforms.chatgpt.registration_engine import generate_password


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        try:
            from platforms.chatgpt.payment import check_subscription_status

            class _A:
                pass

            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.cookies = extra.get("cookies", "")
            status = check_subscription_status(a, proxy=self.config.proxy if self.config else None)
            return status not in ("expired", "invalid", "banned", None)
        except Exception:
            return False

    def register(self, email: str = None, password: str = None) -> Account:
        if not password:
            password = generate_password()

        proxy = self.config.proxy if self.config else None
        extra_config = (self.config.extra or {}) if self.config and getattr(self.config, "extra", None) else {}
        log_fn = getattr(self, "_log_fn", print)

        mailbox = self.mailbox
        mailbox_kind = "mailbox"
        if mailbox is None:
            from core.base_mailbox import TempMailLolMailbox

            mailbox = TempMailLolMailbox(proxy=proxy)
            mailbox._task_control = getattr(self, "_task_control", None)
            mailbox_kind = "tempmail_lol"

        adapter = build_chatgpt_registration_mode_adapter(extra_config)
        result = adapter.run(
            ChatGPTRegistrationContext(
                mailbox=mailbox,
                proxy_url=proxy,
                callback_logger=log_fn,
                email=email,
                password=password,
                extra_config=extra_config,
                mailbox_kind=mailbox_kind,
            )
        )
        if not result or not result.success:
            message = result.error_message if result else "注册失败"
            if result is not None and not result.retryable:
                from core.task_runtime import NonRetryableRegisterError

                raise NonRetryableRegisterError(message)
            raise RuntimeError(message)

        return adapter.build_account(result, password)

    def get_platform_actions(self) -> list:
        return [
            {"id": "probe_local_status", "label": "探测本地状态", "params": []},
            {"id": "check_plus_trial", "label": "检测 Plus 试用", "params": []},
            {"id": "sync_cliproxyapi_status", "label": "同步 CLIProxyAPI 状态", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "backfill_refresh_token", "label": "补 RT", "params": []},
            {"id": "bind_2fa", "label": "绑定 2FA", "params": []},
            {
                "id": "payment_channel_link",
                "label": "渠道提链",
                "params": [
                    {"key": "channel", "label": "渠道", "type": "text"},
                    {"key": "country", "label": "账单国家", "type": "text"},
                    {"key": "currency", "label": "币种", "type": "text"},
                ],
            },
            {
                "id": "payment_channel_pay",
                "label": "渠道支付",
                "params": [
                    {"key": "channel", "label": "渠道", "type": "text"},
                    {"key": "card_id", "label": "卡片 ID", "type": "number"},
                    {"key": "taxfree_state", "label": "免税州", "type": "text"},
                ],
            },
            {
                "id": "upload_cpa",
                "label": "上传 CPA",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_sub2api",
                "label": "上传 Sub2API",
                "params": [
                    {"key": "api_url", "label": "Sub2API API URL", "type": "text"},
                    {"key": "api_key", "label": "Sub2API API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_tm",
                "label": "上传 Team Manager",
                "params": [
                    {"key": "api_url", "label": "TM API URL", "type": "text"},
                    {"key": "api_key", "label": "TM API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_codex_proxy",
                "label": "上传 CodexProxy",
                "params": [
                    {"key": "api_url", "label": "API URL", "type": "text"},
                    {"key": "api_key", "label": "Admin Key", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}
        from services.chatgpt_account_purpose import is_web_pool_account
        web_only = is_web_pool_account(account)
        if web_only and action_id in {
            "upload_cpa", "upload_sub2api", "upload_tm", "upload_codex_proxy",
        }:
            return {"ok": False, "error": "Web 号池账号不会自动外传到 CPA／Sub2API／CodexProxy；如需请用普通账号流程"}

        class _A:
            pass

        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id

        if action_id == "probe_local_status":
            from platforms.chatgpt.status_probe import probe_local_chatgpt_status

            probe_result = probe_local_chatgpt_status(a, proxy=proxy)
            summary = (
                f"认证={probe_result.get('auth', {}).get('state', 'unknown')}, "
                f"订阅={probe_result.get('subscription', {}).get('plan', 'unknown')}, "
                f"Codex={probe_result.get('codex', {}).get('state', 'unknown')}"
            )
            return {
                "ok": True,
                "data": {
                    "message": f"本地状态探测完成：{summary}",
                    "probe": probe_result,
                },
                "account_extra_patch": {
                    "chatgpt_local": probe_result,
                },
            }

        if action_id == "check_plus_trial":
            from platforms.chatgpt.status_probe import (
                PLUS_TRIAL_INCONCLUSIVE,
                probe_plus_trial_status,
            )

            trial = probe_plus_trial_status(a, proxy=proxy)
            conclusive = trial["status"] not in PLUS_TRIAL_INCONCLUSIVE
            return {
                "ok": conclusive,
                "data": {
                    "message": f"Plus 试用检测完成：{trial['label']}",
                    "plus_check": trial,
                },
                "error": "" if conclusive else trial.get("message") or trial["label"],
                # 没查出结论就不落库，免得账号从"未检测"里消失、看着像查过了
                "account_extra_patch": {"plus_check": trial} if conclusive else {},
            }

        if action_id == "sync_cliproxyapi_status":
            from services.cliproxyapi_sync import sync_chatgpt_cliproxyapi_status

            sync_result = sync_chatgpt_cliproxyapi_status(a)
            ok = bool(sync_result.get("uploaded")) and sync_result.get("remote_state") not in {"unreachable", "not_found"}
            summary = (
                f"远端状态={sync_result.get('status') or 'not_found'}, "
                f"探测={sync_result.get('remote_state') or 'not_checked'}"
            )
            return {
                "ok": ok,
                "data": {
                    "message": f"CLIProxyAPI 状态同步完成：{summary}",
                    "sync": sync_result,
                },
                "error": sync_result.get("message") if not ok else "",
                "account_extra_patch": {
                    "sync_statuses": {
                        "cliproxyapi": sync_result,
                    },
                },
            }

        if action_id == "refresh_token":
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            manager = TokenRefreshManager(proxy_url=proxy)
            result = manager.refresh_account(a)
            if result.success:
                return {
                    "ok": True,
                    "data": {
                        "access_token": result.access_token,
                        "refresh_token": result.refresh_token,
                    },
                }
            return {"ok": False, "error": result.error_message}

        if action_id == "backfill_refresh_token":
            from services.chatgpt_rt_backfill import backfill_account_data, build_extra_patch

            result = backfill_account_data(
                email=account.email,
                password=account.password,
                extra=extra,
                token=account.token,
                config=(self.config.extra or {}) if self.config else {},
                proxy=proxy,
                allow_login=str(params.get("allow_login", "1")).lower() not in ("0", "false", "no"),
                log_fn=getattr(self, "_log_fn", None),
            )
            return {
                "ok": result.success,
                "data": {"message": result.summary(), "strategy": result.strategy},
                "error": "" if result.success else result.summary(),
                "account_extra_patch": build_extra_patch(result),
                # 交给调用方决定要不要把账号改判为 deleted（OpenAI 说号已删除/停用）
                "account_deleted": result.account_deleted,
            }

        if action_id == "bind_2fa":
            from services.chatgpt_two_factor import bind_account_two_factor, build_extra_patch

            result = bind_account_two_factor(
                email=account.email,
                password=account.password,
                extra=extra,
                token=account.token,
                config=(self.config.extra or {}) if self.config else {},
                proxy=proxy,
                allow_login=str(params.get("allow_login", "1")).lower() not in ("0", "false", "no"),
                log_fn=getattr(self, "_log_fn", None),
            )
            ok = result.ok or result.already_bound
            return {
                "ok": ok,
                # 密钥只下发这一次，返回给前端让用户当场导入验证器
                "data": {"message": result.summary(), "totp_secret": result.secret},
                "error": "" if ok else result.summary(),
                "account_extra_patch": build_extra_patch(result),
            }

        if action_id == "payment_link":
            from platforms.chatgpt.payment import generate_plus_link, generate_team_link

            plan = params.get("plan", "plus")
            country = params.get("country", "US")
            if plan == "plus":
                url = generate_plus_link(a, proxy=proxy, country=country)
            else:
                url = generate_team_link(
                    a,
                    workspace_name=params.get("workspace_name", "MyTeam"),
                    price_interval=params.get("price_interval", "month"),
                    seat_quantity=int(params.get("seat_quantity", 5) or 5),
                    proxy=proxy,
                    country=country,
                )
            return {"ok": bool(url), "data": {"url": url}}

        if action_id in {"payment_channel_link", "payment_channel_pay"}:
            from services.payment_channels import PaymentAccount
            from services.payment_channels.service import create_link_for_context, pay_for_context

            context = PaymentAccount(
                platform=account.platform,
                account_id=str(account.user_id or ""),
                email=account.email,
                access_token=str(a.access_token or ""),
                session_token=str(a.session_token or ""),
                user_id=str(account.user_id or ""),
                cookies=str(a.cookies or ""),
            )
            options = dict(params or {})
            channel = str(options.pop("channel", "direct") or "direct")
            result = (
                create_link_for_context(context, channel, options=options)
                if action_id == "payment_channel_link"
                else pay_for_context(context, channel, options=options)
            )
            return result.as_dict()

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(
                token_data,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_sub2api":
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            ok, msg = upload_to_sub2api(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager

            ok, msg = upload_to_team_manager(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_codex_proxy":
            upload_type = str(
                params.get("upload_type")
                or (self.config.extra or {}).get("codex_proxy_upload_type")
                or "at"
            ).strip().lower()

            if upload_type == "rt":
                from platforms.chatgpt.cpa_upload import upload_to_codex_proxy

                ok, msg = upload_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            else:
                from platforms.chatgpt.cpa_upload import upload_at_to_codex_proxy

                ok, msg = upload_at_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"未知操作: {action_id}")
