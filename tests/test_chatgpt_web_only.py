"""Web 池注册隔离回归：全为本地 mock，不注册、不租号、不收真实验证码。"""

import os
from types import SimpleNamespace
from unittest import mock

import pytest

from core.base_mailbox import MailboxAccount
from core.task_runtime import SkipCurrentAttemptRequested, StopTaskRequested
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
    resolve_chatgpt_bind_2fa,
    resolve_chatgpt_register_flow,
    resolve_chatgpt_registration_mode,
)
from platforms.chatgpt.protocol.auth_flow import (
    AuthFlow,
    AuthResult,
    PhoneVerificationRequiredError,
)
from platforms.chatgpt.protocol.config import Config
from platforms.chatgpt.registration_engine import (
    REGISTER_FLOW_EMAIL,
    REGISTER_FLOW_PHONE,
    REGISTER_FLOW_PHONE_WITH_EMAIL,
    REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    REGISTRATION_MODE_REFRESH_TOKEN,
    ChatGPTRegistrationEngine,
    RegistrationResult,
)


class _Mailbox:
    def __init__(self):
        self.account = MailboxAccount(
            email="web@example.com",
            account_id="offline-mailbox",
            extra={
                "provider": "microsoft",
                "refresh_token": "mail-rt",
                "client_id": "mail-client",
                "password": "mail-pw",
            },
        )
        self.statuses = []

    def get_email(self):
        return self.account

    def get_current_ids(self, _account):
        return set()

    def wait_for_code(self, _account, **_kwargs):
        return "123456"

    def set_account_status(self, _account, status):
        self.statuses.append(status)


def _engine(web_only=True, **kwargs):
    extra = dict(kwargs.pop("extra_config", {}))
    extra.setdefault("chatgpt_web_only", "1" if web_only else "0")
    return ChatGPTRegistrationEngine(
        mailbox=_Mailbox(), extra_config=extra, log_fn=lambda _message: None, **kwargs
    )


def _result():
    result = AuthResult()
    result.email = "web@example.com"
    result.password = "web-pw"
    result.access_token = "web-at"
    result.session_token = "web-session"
    return result


def _flow(overrides=None):
    # 真正构造 AuthFlow，但 HTTP session 的创建也 mock 掉，保证不会出网。
    with mock.patch(
        "platforms.chatgpt.protocol.auth_flow.create_http_session", return_value=mock.Mock()
    ):
        flow = AuthFlow(
            Config(),
            env_overrides=overrides or {"OPENAI_DISABLE_PHONE_VERIFICATION": "1"},
            sms_callback=mock.Mock(),
        )
    flow._trace_http = mock.Mock()
    return flow


def _response(*, location="", url="https://auth.openai.com/continue", payload=None):
    return SimpleNamespace(
        status_code=302 if location else 200,
        url=url,
        headers={"Location": location} if location else {},
        text="",
        json=lambda: payload or {},
    )


def _prepare_registration(flow, *, is_new=True, otp_response=None):
    flow.check_proxy = mock.Mock(return_value=True)
    flow.warmup = mock.Mock(return_value=True)
    flow.get_csrf_token = mock.Mock(return_value="csrf")
    flow.get_auth_url = mock.Mock(return_value="https://auth.openai.com/authorize?state=test")
    flow.auth_oauth_init = mock.Mock(return_value="device")
    flow.get_sentinel_token = mock.Mock(return_value="sentinel")
    flow.signup = mock.Mock(return_value=is_new)
    flow.register_password = mock.Mock(return_value=True)
    flow.send_otp = mock.Mock()
    flow.kickoff_otp_delivery = mock.Mock(return_value=True)
    flow.verify_otp = mock.Mock(return_value=otp_response or {"continue_url": "/about-you"})
    flow.fetch_client_auth_session_dump = mock.Mock()
    flow.create_account = mock.Mock(return_value="https://auth.openai.com/continue")
    flow._reauthorize_for_session = mock.Mock(return_value="https://chatgpt.com/")
    flow.follow_redirect_chain = mock.Mock(return_value=("", "https://chatgpt.com/"))
    flow.oauth_codex_rt_exchange = mock.Mock()
    flow._consume_callback_for_session = mock.Mock()
    flow.get_auth_session = mock.Mock()
    flow.result = _result()
    return SimpleNamespace(
        create_mailbox=mock.Mock(return_value="web@example.com"),
        wait_for_otp=mock.Mock(return_value="123456"),
        pooled=False,
    )


@pytest.mark.parametrize("flag", ["1", True, "true"])
def test_web_only_forces_email_access_token_and_no_2fa(flag):
    extra = {
        "chatgpt_web_only": flag,
        "chatgpt_registration_mode": "refresh_token",
        "chatgpt_register_flow": "phone_with_email",
        "chatgpt_bind_2fa": "1",
    }
    engine = _engine(
        extra_config=extra,
        mode=REGISTRATION_MODE_REFRESH_TOKEN,
        register_flow=REGISTER_FLOW_PHONE_WITH_EMAIL,
        bind_2fa=True,
    )
    assert engine.mode == REGISTRATION_MODE_ACCESS_TOKEN_ONLY
    assert engine.register_flow == REGISTER_FLOW_EMAIL
    assert engine.bind_2fa is False
    assert resolve_chatgpt_registration_mode(extra) == REGISTRATION_MODE_ACCESS_TOKEN_ONLY
    assert resolve_chatgpt_register_flow(extra) == REGISTER_FLOW_EMAIL
    assert resolve_chatgpt_bind_2fa(extra) is False
    adapter = build_chatgpt_registration_mode_adapter(extra)
    assert (adapter.mode, adapter.register_flow, adapter.bind_2fa) == (
        REGISTRATION_MODE_ACCESS_TOKEN_ONLY, REGISTER_FLOW_EMAIL, False
    )


def test_web_only_never_reads_global_sms_settings_or_uses_injected_controller():
    engine = _engine(extra_config={"sms_enabled": True, "sms_api_key": "offline-key"})
    with mock.patch(
        "platforms.chatgpt.registration_engine.resolve_sms_settings",
        side_effect=AssertionError("must not read global SMS settings"),
    ) as resolve, mock.patch(
        "platforms.chatgpt.registration_engine.build_phone_callback",
        side_effect=AssertionError("must not build a controller"),
    ) as build, mock.patch(
        "platforms.chatgpt.registration_engine.AuthFlow"
    ) as auth_flow:
        assert engine._build_sms_callback() is None
        engine._build_flow(sms_callback=mock.Mock())

    resolve.assert_not_called()
    build.assert_not_called()
    assert auth_flow.call_args.kwargs["sms_callback"] is None
    assert auth_flow.call_args.kwargs["on_session_ready"] is None


def test_web_overrides_mask_process_phone_and_rt_flags_without_mutating_environment():
    environment = {
        "OPENAI_PHONE_NUMBER": "+15555550100",
        "OPENAI_PHONE_OTP": "123456",
        "OPENAI_PHONE_OTP_CMD": "do-not-run",
        "OPENAI_DISABLE_PHONE_VERIFICATION": "0",
        "OAUTH_CODEX_RT_EXCHANGE": "1",
        "OAUTH_CODEX_RT_BEFORE_CALLBACK": "1",
        "OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY": "1",
        "OAUTH_REFRESH_ONLY": "1",
    }
    engine = _engine(extra_config={"chatgpt_phone_number": "+15555550101"})
    with mock.patch.dict(os.environ, environment):
        before = dict(os.environ)
        flow = _flow(engine._env_overrides())
        assert flow._get_env("OPENAI_DISABLE_PHONE_VERIFICATION") == "1"
        for key in ("OPENAI_PHONE_NUMBER", "OPENAI_PHONE_OTP", "OPENAI_PHONE_OTP_CMD"):
            assert flow._get_env(key) == ""
        for key in (
            "OAUTH_CODEX_RT_EXCHANGE", "OAUTH_CODEX_RT_BEFORE_CALLBACK",
            "OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY", "OAUTH_REFRESH_ONLY",
        ):
            assert flow._get_env(key) == "0"
        assert dict(os.environ) == before
        legacy = _engine(web_only=False)._env_overrides()
        assert "OPENAI_DISABLE_PHONE_VERIFICATION" not in legacy
        assert "OPENAI_PHONE_NUMBER" not in legacy


@pytest.mark.parametrize("handler", [
    "_handle_add_phone_verification", "_handle_add_phone_via_sms", "_handle_add_phone_via_env",
])
def test_phone_handlers_reject_before_sms_or_environment_fallback(handler):
    flow = _flow()
    flow._add_phone_send = mock.Mock()
    flow._wait_phone_otp = mock.Mock()
    controller = flow._sms_callback
    with mock.patch.dict(os.environ, {
        "OPENAI_PHONE_NUMBER": "+15555550100", "OPENAI_PHONE_OTP": "123456",
        "OPENAI_PHONE_OTP_CMD": "do-not-run",
    }), mock.patch(
        "platforms.chatgpt.protocol.auth_flow.subprocess.check_output"
    ) as command:
        with pytest.raises(PhoneVerificationRequiredError, match="phone challenge"):
            getattr(flow, handler)("https://auth.openai.com/add-phone")
    assert flow._phone_verification_required is True
    assert controller.mock_calls == []
    flow.session.get.assert_not_called()
    flow.session.post.assert_not_called()
    flow._add_phone_send.assert_not_called()
    flow._wait_phone_otp.assert_not_called()
    command.assert_not_called()


@pytest.mark.parametrize("step", [
    {"page": {"type": "add_phone"}},
    {"page": {"type": "phone_otp_verification"}},
    {"page": {"type": "phone_verification"}},
    {"continue_url": "/add-phone"},
    {"continue_url": "/phone-verification"},
    {"page": {"type": "external_url", "payload": {"url": "/add-phone"}}},
])
def test_phone_guard_marks_challenge_persistently(step):
    flow = _flow()
    with pytest.raises(PhoneVerificationRequiredError):
        flow._guard_phone_step(step)
    assert flow._phone_verification_required is True
    with pytest.raises(PhoneVerificationRequiredError):
        flow._guard_phone_step({"continue_url": "https://chatgpt.com/"})


@pytest.mark.parametrize("method,args", [
    ("verify_otp", ("123456",)),
    ("login_password_verify", ("offline-pw",)),
    ("submit_mfa_totp", ("123456", "offline-challenge")),
    ("signup", ("web@example.com", "sentinel")),
    ("create_account", ()),
])
def test_auth_step_phone_challenge_cannot_be_swallowed_by_json_fallback(method, args):
    flow = _flow()
    flow.session.post.return_value = _response(payload={"page": {"type": "add_phone"}})
    with pytest.raises(PhoneVerificationRequiredError):
        getattr(flow, method)(*args)
    assert flow._phone_verification_required is True


@pytest.mark.parametrize("retry_first", [False, True])
def test_new_account_checks_otp_result_before_creating_account(retry_first):
    flow = _flow(_engine()._env_overrides())
    step = {"page": {"type": "add_phone"}}
    provider = _prepare_registration(flow, otp_response=step)
    if retry_first:
        flow.verify_otp.side_effect = [RuntimeError("401 invalid OTP"), step]
    with pytest.raises(PhoneVerificationRequiredError):
        flow.run_register(provider)
    assert flow._phone_verification_required is True
    flow.create_account.assert_not_called()
    flow._reauthorize_for_session.assert_not_called()
    flow.oauth_codex_rt_exchange.assert_not_called()


def test_existing_account_checks_otp_challenge_without_url():
    flow = _flow(_engine()._env_overrides())
    provider = _prepare_registration(
        flow, is_new=False, otp_response={"page": {"type": "phone_otp_verification"}}
    )
    with pytest.raises(PhoneVerificationRequiredError):
        flow.run_register(provider)
    flow._reauthorize_for_session.assert_not_called()
    flow.get_auth_session.assert_not_called()


@pytest.mark.parametrize("source", ["start", "location", "final_url"])
def test_redirect_phone_challenge_stops_before_homepage_fallback(source):
    flow = _flow()
    if source == "start":
        start = "https://auth.openai.com/add-phone"
    else:
        start = "https://auth.openai.com/continue"
        flow.session.get.return_value = (
            _response(location="/add-phone") if source == "location"
            else _response(url="https://auth.openai.com/phone-verification")
        )
    with pytest.raises(PhoneVerificationRequiredError):
        flow.follow_redirect_chain(start)
    assert flow._phone_verification_required is True
    assert flow.session.get.call_count == (0 if source == "start" else 1)


@pytest.mark.parametrize("method,args", [
    ("_reauthorize_for_session", ("https://auth.openai.com/authorize?state=test",)),
    ("_consume_callback_for_session", ("https://chatgpt.com/api/auth/callback/openai?code=test",)),
])
def test_swallowed_redirect_error_still_blocks_session_and_engine_salvage(method, args):
    flow = _flow(_engine()._env_overrides())
    flow.result = _result()
    flow.session.get.return_value = _response(location="/add-phone")
    # 这两个旧方法有宽泛 except；标记必须留下，不能以它们的返回值判断成功。
    assert not getattr(flow, method)(*args)
    assert flow._phone_verification_required is True
    with pytest.raises(PhoneVerificationRequiredError):
        flow.get_auth_session()
    assert flow.session.get.call_count == 1
    result = _engine()._salvage(flow, RuntimeError("late error"))
    assert result.success is False
    assert "phone challenge" in result.error_message
    assert result.access_token == ""


def test_web_register_keeps_web_authorize_and_session_but_never_codex():
    engine = _engine()
    flow = _flow(engine._env_overrides())
    provider = _prepare_registration(flow)
    with mock.patch.dict(os.environ, {
        "OAUTH_CODEX_RT_EXCHANGE": "1", "OAUTH_CODEX_RT_BEFORE_CALLBACK": "1",
        "OAUTH_REFRESH_ONLY": "1",
    }):
        result = flow.run_register(provider)
    assert result.access_token == "web-at"
    assert result.refresh_token == ""
    flow.auth_oauth_init.assert_called_once()
    assert flow.get_auth_session.call_count == 2
    flow.oauth_codex_rt_exchange.assert_not_called()


@pytest.mark.parametrize("late_error", [False, True])
def test_engine_refuses_phone_marked_result_even_when_credentials_exist(late_error):
    engine = _engine()
    flow = SimpleNamespace(result=_result(), _phone_verification_required=True)
    flow.run_register = mock.Mock(
        return_value=flow.result,
        side_effect=RuntimeError("late failure") if late_error else None,
    )
    provider = SimpleNamespace(account=engine.mailbox.account, _mailbox=engine.mailbox)
    with mock.patch.object(engine, "_build_flow", return_value=flow), mock.patch.object(
        engine, "_build_mail_provider", return_value=provider
    ):
        result = engine.run()
    assert result.success is False
    assert "phone challenge" in result.error_message
    assert result.access_token == result.session_token == result.refresh_token == ""
    assert engine.mailbox.statuses == ["failed"]


def test_protocol_final_guard_refuses_swallowed_phone_mark():
    flow = _flow(_engine()._env_overrides())
    provider = _prepare_registration(flow)
    flow.get_auth_session.side_effect = lambda: setattr(flow, "_phone_verification_required", True)
    with pytest.raises(PhoneVerificationRequiredError):
        flow.run_register(provider)


@pytest.mark.parametrize("exception_type", [StopTaskRequested, SkipCurrentAttemptRequested])
@pytest.mark.parametrize("web_only", [False, True])
def test_stop_and_skip_propagate_instead_of_salvaging_credentials(exception_type, web_only):
    engine = _engine(web_only=web_only)
    interruption = exception_type()
    flow = SimpleNamespace(
        result=_result(), _phone_verification_required=False,
        run_register=mock.Mock(side_effect=interruption),
    )
    with mock.patch.object(engine, "_build_flow", return_value=flow), mock.patch.object(
        engine, "_salvage", wraps=engine._salvage
    ) as salvage:
        with pytest.raises(exception_type) as raised:
            engine.run()
    assert raised.value is interruption
    salvage.assert_not_called()
    assert engine.mailbox.statuses == []
    with pytest.raises(exception_type):
        engine._salvage(flow, interruption)


@pytest.mark.parametrize("exception_type", [StopTaskRequested, SkipCurrentAttemptRequested])
def test_phone_mode_stop_is_not_salvaged(exception_type):
    engine = _engine(web_only=False, register_flow=REGISTER_FLOW_PHONE)
    flow = SimpleNamespace(result=_result(), run_phone_register=mock.Mock(side_effect=exception_type()))
    with mock.patch.object(engine, "_build_flow", return_value=flow), mock.patch.object(
        engine, "_build_sms_callback", return_value=mock.Mock()
    ):
        with pytest.raises(exception_type):
            engine.run()


@pytest.mark.parametrize("web_only,attempts", [(True, 1), (False, 3)])
def test_adapter_mailbox_attempt_budget_is_isolated(web_only, attempts):
    adapter = build_chatgpt_registration_mode_adapter({})
    extra = {"chatgpt_web_only": "1" if web_only else "0"}
    context = ChatGPTRegistrationContext(
        mailbox=_Mailbox(), proxy_url=None, callback_logger=lambda _message: None,
        email=None, password=None, extra_config=extra,
    )
    with mock.patch(
        "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine"
    ) as engine_type:
        engine_type.return_value.run.return_value = RegistrationResult(
            success=False, error_message="IMAP authentication failed"
        )
        assert adapter.run(context).success is False
    assert engine_type.call_count == attempts
    if web_only:
        kwargs = engine_type.call_args.kwargs
        assert kwargs["mode"] == REGISTRATION_MODE_ACCESS_TOKEN_ONLY
        assert kwargs["register_flow"] == REGISTER_FLOW_EMAIL
        assert kwargs["bind_2fa"] is False
    assert context.extra_config == extra


def test_adapter_propagates_stop_without_starting_another_attempt():
    adapter = build_chatgpt_registration_mode_adapter({"chatgpt_web_only": "1"})
    context = ChatGPTRegistrationContext(
        mailbox=_Mailbox(), proxy_url=None, callback_logger=lambda _message: None,
        email=None, password=None, extra_config={},
    )
    with mock.patch(
        "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine"
    ) as engine_type:
        engine_type.return_value.run.side_effect = StopTaskRequested()
        with pytest.raises(StopTaskRequested):
            adapter.run(context)
    engine_type.assert_called_once()
    assert engine_type.call_args.kwargs["extra_config"]["chatgpt_web_only"] == "1"


@pytest.mark.parametrize("chatgpt_rt", ["", "real-chatgpt-rt"])
def test_web_metadata_preserves_mailbox_credentials_without_replacing_chatgpt_tokens(chatgpt_rt):
    adapter = build_chatgpt_registration_mode_adapter({"chatgpt_web_only": "1"})
    metadata = {"refresh_token": "mail-rt", "client_id": "mail-client", "password": "mail-pw"}
    result = RegistrationResult(
        success=True, email="web@example.com", access_token="web-at",
        refresh_token=chatgpt_rt, metadata=metadata.copy(),
    )
    account = adapter.build_account(result, fallback_password="web-pw")
    assert account.extra["refresh_token"] == chatgpt_rt
    assert "client_id" not in account.extra
    assert "password" not in account.extra
    assert account.extra["outlook_refresh_token"] == "mail-rt"
    assert account.extra["outlook_client_id"] == "mail-client"
    assert account.extra["outlook_password"] == "mail-pw"
    assert result.metadata == metadata


@pytest.mark.parametrize("late_error", [False, True])
def test_web_only_requires_access_token_not_just_rt_or_session(late_error):
    result = _result()
    result.access_token = ""
    result.session_token = "only-session"
    result.refresh_token = "only-rt"
    engine = _engine()
    flow = SimpleNamespace(
        result=result, _phone_verification_required=False,
        run_register=mock.Mock(return_value=result, side_effect=RuntimeError("late") if late_error else None),
    )
    provider = SimpleNamespace(account=engine.mailbox.account, _mailbox=engine.mailbox)
    with mock.patch.object(engine, "_build_flow", return_value=flow), mock.patch.object(
        engine, "_build_mail_provider", return_value=provider
    ):
        outcome = engine.run()
    assert outcome.success is False
    assert "access_token" in outcome.error_message


def test_legacy_mode_keeps_sms_env_salvage_and_metadata_behavior():
    engine = _engine(web_only=False, register_flow=REGISTER_FLOW_PHONE_WITH_EMAIL, bind_2fa=True)
    assert engine.mode == REGISTRATION_MODE_REFRESH_TOKEN
    assert engine.register_flow == REGISTER_FLOW_PHONE_WITH_EMAIL
    assert engine.bind_2fa is True
    controller = mock.Mock()
    with mock.patch(
        "platforms.chatgpt.registration_engine.resolve_sms_settings", return_value={"sms_enabled": True}
    ) as resolve, mock.patch(
        "platforms.chatgpt.registration_engine.build_phone_callback", return_value=controller
    ):
        assert engine._build_sms_callback() is controller
    resolve.assert_called_once()

    flow = _flow({"OPENAI_DISABLE_PHONE_VERIFICATION": "0"})
    flow.result = _result()
    flow._sms_callback = None
    flow._handle_add_phone_via_env = mock.Mock(return_value="https://chatgpt.com/")
    assert flow._handle_add_phone_verification("/add-phone") == "https://chatgpt.com/"
    flow._handle_add_phone_via_env.assert_called_once()
    assert engine._salvage(flow, RuntimeError("late error")).success is True

    adapter = build_chatgpt_registration_mode_adapter({})
    account = adapter.build_account(RegistrationResult(
        success=True, refresh_token="chatgpt-rt",
        metadata={"refresh_token": "mail-rt", "client_id": "mail-client"},
    ), fallback_password="pw")
    assert account.extra["refresh_token"] == "mail-rt"
    assert account.extra["client_id"] == "mail-client"
    assert "outlook_refresh_token" not in account.extra
