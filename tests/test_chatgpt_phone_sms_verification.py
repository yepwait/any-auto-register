"""Phone registration must persist verified SMS evidence, not merely receipt of a code.

All controller and protocol operations are local fakes; no phone is rented.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from platforms.chatgpt.protocol.auth_flow import AuthFlow, AuthResult
from platforms.chatgpt.protocol.phone_flow import _PhoneUnusable
from platforms.chatgpt.registration_engine import RegistrationResult


def _flow():
    flow = AuthFlow.__new__(AuthFlow)
    flow.result = AuthResult()
    flow._normalize_continue_url = lambda url: url
    return flow


def _controller(codes, *, country="56"):
    pending = iter(codes)
    return SimpleNamespace(
        activation=SimpleNamespace(country=country),
        get_code=lambda timeout: next(pending, ""),
        mark_code_failed=lambda reason: None,
    )


@pytest.mark.parametrize("country", ["56", None])
def test_validated_phone_sms_survives_registration_result(country):
    flow = _flow()
    flow._phone_otp_validate = lambda code, referer: {
        "continue_url": "https://auth.openai.com/about-you"
    }

    url = flow._wait_and_validate_sms(
        "+15555550100", _controller(["123456"], country=country), 40, 1, ""
    )

    assert url == "https://auth.openai.com/about-you"
    evidence = flow.result.sms_verification
    assert evidence["used"] is True
    assert evidence["verified"] is True
    assert evidence["country"] == (country or "")
    assert datetime.fromisoformat(evidence["at"]).tzinfo == timezone.utc
    registration = RegistrationResult.from_auth_result(flow.result, source="phone_register")
    assert registration.metadata["sms_verification"] == evidence


def test_received_but_rejected_code_does_not_mark_verified():
    flow = _flow()

    def reject(code, referer):
        raise RuntimeError("invalid otp")

    flow._phone_otp_validate = reject
    with pytest.raises(_PhoneUnusable):
        flow._wait_and_validate_sms("+15555550100", _controller(["123456"]), 40, 1, "")

    assert flow._phone_sms_ever_received is True
    assert flow.result.sms_verification == {}


def test_no_sms_does_not_mark_verified():
    flow = _flow()
    flow._phone_otp_validate = lambda **kwargs: pytest.fail("must not validate without SMS")

    with pytest.raises(_PhoneUnusable):
        flow._wait_and_validate_sms("+15555550100", _controller([]), 40, 1, "")

    assert not flow._phone_sms_ever_received
    assert flow.result.sms_verification == {}


def test_retry_records_evidence_only_after_validation_succeeds():
    flow = _flow()
    validated = []

    def validate(code, referer):
        assert flow.result.sms_verification == {}
        validated.append(code)
        if code == "111111":
            raise RuntimeError("invalid otp")
        return {"continue_url": "https://auth.openai.com/about-you"}

    flow._phone_otp_validate = validate
    flow._wait_and_validate_sms(
        "+15555550100", _controller(["111111", "222222"]), 40, 2, ""
    )

    assert validated == ["111111", "222222"]
    assert flow.result.sms_verification["verified"] is True


def test_skipping_sms_page_is_not_evidence_of_verification():
    flow = _flow()
    flow._wait_and_validate_sms = lambda *args: pytest.fail("must not request SMS on forward page")

    url = flow._continue_after_phone_verified(
        "+15555550100", _controller([]), 40, 1,
        "about_you", "https://auth.openai.com/about-you",
    )

    assert url == "https://auth.openai.com/about-you"
    assert flow.result.sms_verification == {}
