import unittest

from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    classify_local_probe_state,
    classify_remote_sync_state,
    is_account_deactivated_message,
)


class DummyAccount:
    def __init__(self, status="registered"):
        self.status = status


class ChatGPTAccountStateTests(unittest.TestCase):
    def test_local_401_marks_invalid(self):
        account = DummyAccount()
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "access_token_invalidated",
                    "http_status": 401,
                    "error_code": "token_invalidated",
                    "message": "invalidated",
                }
            },
        )
        self.assertEqual(reason, "auth_401")
        self.assertEqual(account.status, "invalid")

    def test_remote_401_marks_invalid(self):
        self.assertEqual(
            classify_remote_sync_state(
                {
                    "remote_state": "access_token_invalidated",
                    "last_probe_status_code": 401,
                    "last_probe_error_code": "token_invalidated",
                    "last_probe_message": "invalidated",
                }
            ),
            "remote_401",
        )

    def test_remote_relogin_marks_account_invalid_even_when_probe_is_200(self):
        account = DummyAccount(status="registered")

        reason = apply_chatgpt_status_policy(
            account,
            remote_sync={
                "remote_state": "relogin_required",
                "last_probe_status_code": 200,
            },
        )

        self.assertEqual(reason, "remote_relogin_required")
        self.assertEqual(account.status, "invalid")

    def test_payment_and_quota_do_not_mark_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "codex": {"state": "payment_required", "http_status": 403, "message": "payment required"},
                }
            ),
            "",
        )
        self.assertEqual(
            classify_remote_sync_state(
                {
                    "remote_state": "quota_exhausted",
                    "last_probe_status_code": 429,
                    "last_probe_error_code": "",
                    "last_probe_message": "usage limit reached",
                }
            ),
            "",
        )

    def test_local_200_restores_historical_invalid_status(self):
        account = DummyAccount(status="invalid")

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "codex": {"state": "usable", "http_status": 200},
            },
        )

        self.assertEqual(reason, "local_valid")
        self.assertEqual(account.status, "registered")

    def test_remote_usable_200_restores_historical_invalid_status(self):
        account = DummyAccount(status="invalid")

        reason = apply_chatgpt_status_policy(
            account,
            remote_sync={"remote_state": "usable", "last_probe_status_code": 200},
        )

        self.assertEqual(reason, "remote_valid")
        self.assertEqual(account.status, "registered")

    def test_inconclusive_probe_keeps_historical_invalid_status(self):
        account = DummyAccount(status="invalid")

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={"auth": {"state": "probe_failed", "http_status": 0}},
        )

        self.assertEqual(reason, "")
        self.assertEqual(account.status, "invalid")

    def test_deactivated_message_marks_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "banned_like",
                        "http_status": 403,
                        "error_code": "account_deactivated",
                        "message": "You do not have an account because it has been deleted or deactivated.",
                    }
                }
            ),
            "auth_deactivated",
        )

    def test_is_account_deactivated_message_matches_openai_wording(self):
        self.assertTrue(
            is_account_deactivated_message(
                "",
                "403 - You do not have an account because it has been deleted or deactivated."
                " If you believe this was an error, please contact us through our help center.",
            )
        )
        self.assertTrue(is_account_deactivated_message("account_deleted", ""))
        self.assertFalse(is_account_deactivated_message("token_invalidated", "invalidated"))
        self.assertFalse(is_account_deactivated_message("", "密码错误"))
        self.assertFalse(is_account_deactivated_message("", "403 Forbidden"))

    def test_local_deactivated_message_marks_deleted(self):
        account = DummyAccount(status="registered")

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "banned_like",
                    "http_status": 403,
                    "error_code": "account_deactivated",
                    "message": (
                        "403 - You do not have an account because it has been deleted or "
                        "deactivated. If you believe this was an error, please contact us "
                        "through our help center."
                    ),
                }
            },
        )

        self.assertEqual(reason, "auth_deactivated")
        self.assertEqual(account.status, "deleted")

    def test_remote_deactivated_message_marks_deleted(self):
        account = DummyAccount(status="registered")

        reason = apply_chatgpt_status_policy(
            account,
            remote_sync={
                "remote_state": "account_deactivated",
                "last_probe_status_code": 403,
                "last_probe_error_code": "",
                "last_probe_message": (
                    "You do not have an account because it has been deleted or deactivated."
                ),
            },
        )

        self.assertEqual(reason, "remote_deactivated")
        self.assertEqual(account.status, "deleted")

    def test_plain_403_stays_invalid_not_deleted(self):
        account = DummyAccount(status="registered")

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "banned_like",
                    "http_status": 403,
                    "error_code": "",
                    "message": "Forbidden",
                }
            },
        )

        self.assertEqual(reason, "auth_403")
        self.assertEqual(account.status, "invalid")

    def test_local_200_restores_historical_deleted_status(self):
        account = DummyAccount(status="deleted")

        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "codex": {"state": "usable", "http_status": 200},
            },
        )

        self.assertEqual(reason, "local_valid")
        self.assertEqual(account.status, "registered")


if __name__ == "__main__":
    unittest.main()
