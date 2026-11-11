from __future__ import annotations

from services import inspection_service as service


def test_parse_statuses_drops_unknown_and_uses_defaults():
    assert service._parse_statuses("registered,unknown, trial") == ("registered", "trial")
    assert service._parse_statuses("") == service.DEFAULT_STATUSES


def test_update_settings_normalizes_values(monkeypatch):
    saved: dict[str, str] = {}

    monkeypatch.setattr(service.config_store, "set_many", lambda data: saved.update(data))
    monkeypatch.setattr(
        service.config_store,
        "get",
        lambda key, default="": saved.get(key, default),
    )

    settings = service.update_settings(
        {
            "inspection_enabled": True,
            "inspection_interval_minutes": 0,
            "inspection_concurrency": 999,
            "inspection_delay_seconds": -1,
            "inspection_account_statuses": ["registered", "nope"],
            "inspection_chat2api_token": "secret-token",
        }
    )

    assert saved["inspection_enabled"] == "1"
    assert saved["inspection_interval_minutes"] == "1"
    assert saved["inspection_concurrency"] == "20"
    assert saved["inspection_delay_seconds"] == "0.0"
    assert saved["inspection_account_statuses"] == "registered"
    assert settings.chat2api_token == "secret-token"


def test_update_settings_does_not_clear_chat2api_token(monkeypatch):
    saved: dict[str, str] = {"inspection_chat2api_token": "existing"}
    monkeypatch.setattr(service.config_store, "set_many", lambda data: saved.update(data))
    monkeypatch.setattr(
        service.config_store,
        "get",
        lambda key, default="": saved.get(key, default),
    )

    settings = service.update_settings({"inspection_chat2api_token": ""})

    assert saved["inspection_chat2api_token"] == "existing"
    assert settings.chat2api_token == "existing"


def test_chat2api_url_rejects_non_http_urls():
    try:
        service._validate_webhook_url("file:///tmp/tokens.json")
    except ValueError as exc:
        assert "URL" in str(exc)
    else:
        raise AssertionError("invalid URL was accepted")


def test_public_config_masks_chat2api_token():
    settings = service.InspectionSettings(chat2api_token="secret-token")
    public = settings.public_dict()

    assert public["chat2api_token_configured"] is True
    assert public["chat2api_token_masked"] == "sec***ken"
    assert "secret-token" not in str(public)


def test_chat2api_protocol_normalization():
    assert service._normalize_chat2api_protocol("WEBHOOK") == "webhook"
    assert service._normalize_chat2api_protocol("chatgpt2api") == "chatgpt2api"
    assert service._normalize_chat2api_protocol("unknown") == "chatgpt2api"


def test_only_sms_verified_setting_defaults_off_and_round_trips(monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr(service.config_store, "set_many", saved.update)
    monkeypatch.setattr(service.config_store, "get", lambda key, default="": saved.get(key, default))

    settings = service.update_settings({"inspection_only_sms_verified": "1"})
    assert settings.only_sms_verified is True
    assert settings.public_dict()["only_sms_verified"] is True

    settings = service.update_settings({"inspection_only_sms_verified": "0"})
    assert settings.only_sms_verified is False


def test_select_account_ids_only_sms_verified_filters_before_limit(monkeypatch):
    class FakeRow:
        def __init__(self, row_id, verified):
            self.id = row_id
            self.email = f"user{row_id}@example.com"
            self._verified = verified

        def get_extra(self):
            return {"sms_verification": {"verified": self._verified}}

    rows = [FakeRow(1, False), FakeRow(2, True), FakeRow(3, True)]

    class FakeResult:
        def order_by(self, *_args):
            return self

        def all(self):
            return rows

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def exec(self, *_args):
            return FakeResult()

    monkeypatch.setattr(service, "Session", lambda *_args, **_kwargs: FakeSession())
    found, missing = service.select_account_ids(
        statuses=["registered"],
        only_sms_verified=True,
        max_accounts=1,
    )
    assert found == [2]
    assert missing == []


def test_chat2api_protocol_payload_modes(monkeypatch):
    calls = []

    class Response:
        status_code = 200

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(service.requests, "post", fake_post)
    account = service.SimpleNamespace(
        email="a@example.com", user_id="u", token="at", extra={"access_token": "at"},
        access_token="at", refresh_token="rt", id_token="id", session_token="", client_id="cid",
    )
    for protocol in ("chatgpt2api", "webhook"):
        ok, _ = service._upload_chat2api(
            account,
            {"auth": {}, "subscription": {}, "codex": {}},
            service.InspectionSettings(chat2api_enabled=True, chat2api_protocol=protocol, chat2api_url="https://example.com/import"),
        )
        assert ok is True
    assert list(calls[0]) == ["accounts"]
    assert isinstance(calls[1], dict) and "platform" in calls[1]
