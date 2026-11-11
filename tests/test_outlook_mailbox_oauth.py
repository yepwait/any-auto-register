import unittest
from email.message import EmailMessage
from unittest import mock

from core.base_mailbox import (
    MailboxAccount,
    OutlookImapMailboxBackend,
    OutlookMailbox,
    create_mailbox,
)


class _SelectFallbackImap:
    def __init__(self):
        self.calls = []

    def select(self, folder, readonly=False):
        self.calls.append((folder, readonly))
        if readonly:
            raise RuntimeError("EXAMINE command error: BAD Command Argument Error. 12")
        return "OK", []


class _FakeResponse:
    def __init__(self, status_code, payload=None, text="", json_error=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""
        self.content = b"{}" if payload is not None or json_error is not None else b""
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return dict(self._payload)


def _outlook_message(code: str, date: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = "Your temporary ChatGPT verification code"
    message["Date"] = date
    message.set_content(f"Your verification code is {code}")
    return message.as_bytes()


class _FakeOutlookImap:
    """备注：按文件夹返回 UID 和邮件，复现 Outlook 的目录选择行为。"""

    def __init__(self, messages_by_folder=None, fail_folders=None):
        self.messages_by_folder = messages_by_folder or {}
        self.fail_folders = set(fail_folders or set())
        self.selected = ""
        self.selections = []

    def select(self, folder, readonly=True):
        self.selections.append((folder, readonly))
        if folder in self.fail_folders:
            raise RuntimeError("folder unavailable")
        self.selected = folder
        return "OK", [b"1"]

    def uid(self, command, *args):
        messages = self.messages_by_folder.get(self.selected, [])
        if command == "search":
            return "OK", [b" ".join(str(uid).encode() for uid, _ in messages)]
        if command == "fetch":
            target = int(args[0])
            raw = next(raw for uid, raw in messages if uid == target)
            return "OK", [(b"RFC822", raw)]
        raise AssertionError(f"unexpected UID command: {command}")

    def logout(self):
        return "BYE", [b""]


class OutlookMailboxOAuthTests(unittest.TestCase):
    def test_imap_baseline_quotes_folders_and_keeps_partial_results(self):
        mailbox = OutlookMailbox(backend="imap")
        mailbox._imap_folder_names = ["INBOX", "Deleted Items", "Trash"]
        fake_imap = _FakeOutlookImap(
            messages_by_folder={
                '"INBOX"': [(1, b"")],
                '"Deleted Items"': [(2, b"")],
            },
            fail_folders={'"Trash"'},
        )
        account = MailboxAccount(email="demo@outlook.com", extra={})

        with mock.patch.object(mailbox, "_open_imap", return_value=fake_imap):
            ids = OutlookImapMailboxBackend(mailbox).get_current_ids(account)

        self.assertEqual(ids, {"INBOX:1", "Deleted Items:2"})
        self.assertIn(('"Deleted Items"', True), fake_imap.selections)

    def test_imap_wait_for_code_skips_message_older_than_otp_request(self):
        mailbox = OutlookMailbox(backend="imap")
        mailbox._imap_folder_names = ["INBOX"]
        fake_imap = _FakeOutlookImap(
            messages_by_folder={
                '"INBOX"': [
                    (1, _outlook_message("111111", "Thu, 27 Aug 2026 03:00:00 +0000")),
                    (2, _outlook_message("222222", "Thu, 27 Aug 2026 05:00:00 +0000")),
                ]
            }
        )
        account = MailboxAccount(email="demo@outlook.com", extra={})

        with mock.patch.object(mailbox, "_open_imap", return_value=fake_imap):
            code = OutlookImapMailboxBackend(mailbox).wait_for_code(
                account,
                timeout=1,
                before_ids=set(),
                otp_sent_at=1787803200,
            )

        self.assertEqual(code, "222222")

    def test_create_mailbox_outlook_defaults_to_graph_backend(self):
        mailbox = create_mailbox("outlook", extra={})

        self.assertIsInstance(mailbox, OutlookMailbox)
        self.assertEqual(mailbox._backend_name, "graph")

    @mock.patch("imaplib.IMAP4_SSL", side_effect=OSError("connection refused"))
    def test_imap_login_error_keeps_host_and_auth_method(self, _imap_ssl):
        mailbox = OutlookMailbox(backend="imap")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={"password": "app-password"},
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"outlook\.live\.com:password:OSError: connection refused.*outlook\.office365\.com:password",
        ):
            mailbox._open_imap(account)

    def test_graph_backend_falls_back_to_imap_scope_token(self):
        mailbox = OutlookMailbox(backend="graph")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "_oauth_token_cache": {"graph": {"scope_label": "imap_new"}},
            },
        )
        with mock.patch.object(
            mailbox,
            "_get_oauth_access_token",
            return_value="opaque-imap-token",
        ):
            backend = mailbox._resolve_backend(account)

        self.assertEqual(backend.backend_name, "imap")

    def test_create_mailbox_outlook_does_not_inherit_registration_proxy(self):
        # 备注：注册代理只服务注册流量，微软 OAuth/Graph/IMAP 与参考项目一致走直连。
        mailbox = create_mailbox(
            "microsoft",
            extra={},
            proxy="proxy-user:proxy-pass@proxy.example.test:10000",
        )

        self.assertIsNone(mailbox._proxy)

    @mock.patch("requests.post")
    def test_fetch_oauth_token_graph_backend_prefers_graph_scope(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")

        responses = [
            _FakeResponse(
                400,
                text='{"error":"invalid_grant","error_description":"scopes requested are unauthorized"}',
            ),
            _FakeResponse(
                200,
                payload={"access_token": "access-token-demo"},
                text='{"access_token":"access-token-demo"}',
            ),
        ]
        mock_post.side_effect = responses

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "access-token-demo")
        self.assertEqual(mock_post.call_count, 2)

        first_scope = mock_post.call_args_list[0].kwargs["data"].get("scope", "")
        second_scope = mock_post.call_args_list[1].kwargs["data"].get("scope", "")
        self.assertEqual(
            first_scope,
            "https://graph.microsoft.com/.default",
        )
        self.assertEqual(
            second_scope,
            "https://outlook.office.com/.default offline_access",
        )

    @mock.patch("requests.post")
    def test_fetch_oauth_token_imap_backend_prefers_imap_scope(self, mock_post):
        mailbox = OutlookMailbox(
            token_endpoint="https://token.example.test",
            backend="imap",
        )
        mock_post.side_effect = [
            _FakeResponse(
                200,
                payload={"access_token": "imap-token"},
                text='{"access_token":"imap-token"}',
            ),
        ]

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "imap-token")
        self.assertEqual(
            mock_post.call_args.kwargs["data"].get("scope", ""),
            "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        )

    @mock.patch("requests.post")
    def test_probe_oauth_availability_normalizes_proxy_without_scheme(self, mock_post):
        # 备注：复现代理池返回 user:pass@host:port 时 OAuth 请求被 requests 拒绝的问题。
        mailbox = OutlookMailbox(
            token_endpoint="https://token.example.test",
            proxy="proxy-user:proxy-pass@proxy.example.test:10000",
        )
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "ok-token"},
            text='{"access_token":"ok-token"}',
        )

        result = mailbox.probe_oauth_availability(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            mock_post.call_args.kwargs["proxies"],
            {
                "http": "http://proxy-user:proxy-pass@proxy.example.test:10000",
                "https": "http://proxy-user:proxy-pass@proxy.example.test:10000",
            },
        )

    @mock.patch("requests.post")
    def test_probe_oauth_availability_detects_service_abuse_mode(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.return_value = _FakeResponse(
            400,
            text='{"error":"invalid_grant","error_description":"User account is found to be in service abuse mode."}',
        )

        result = mailbox.probe_oauth_availability(
            email="demo@hotmail.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "service_abuse_mode")
        self.assertIn("service abuse mode", result["message"])

    @mock.patch("requests.post")
    def test_probe_oauth_availability_returns_ok_when_access_token_is_obtained(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "ok-token", "expires_in": 3600},
            text='{"access_token":"ok-token","expires_in":3600}',
        )

        result = mailbox.probe_oauth_availability(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["access_token"], "ok-token")

    @mock.patch("requests.post")
    @mock.patch("requests.request")
    def test_wait_for_code_uses_graph_backend_by_default(self, mock_request, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
            },
        )
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "graph-token", "expires_in": 3600},
            text='{"access_token":"graph-token","expires_in":3600}',
        )
        mock_request.return_value = _FakeResponse(
            200,
            payload={
                "value": [
                    {
                        "id": "message-1",
                        "subject": "OpenAI verification code",
                        "bodyPreview": "Your verification code is 123456",
                    }
                ]
            },
        )

        code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "123456")
        self.assertIn(
            "/me/mailFolders/inbox/messages",
            str(mock_request.call_args.args[1]),
        )

    @mock.patch("requests.get")
    def test_wait_for_code_uses_mailapi_backend_for_mailapi_account(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mailapi.icu/key?type=html&orderNo=abc123",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text="<html><body>Your OpenAI verification code is 246810</body></html>",
        )

        code = mailbox.wait_for_code(
            account,
            timeout=5,
            before_ids=set(),
        )

        self.assertEqual(code, "246810")
        self.assertTrue(mock_get.call_count >= 1)
        self.assertEqual(
            mock_get.call_args.kwargs.get("timeout"),
            15,
        )

    @mock.patch("requests.post")
    @mock.patch("requests.request")
    def test_wait_for_code_reads_deleteditems_folder_when_inbox_has_no_new_code(self, mock_request, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
            },
        )
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "graph-token", "expires_in": 3600},
            text='{"access_token":"graph-token","expires_in":3600}',
        )
        mock_request.side_effect = [
            _FakeResponse(200, payload={"value": []}),
            _FakeResponse(200, payload={"value": []}),
            _FakeResponse(
                200,
                payload={
                    "value": [
                        {
                            "id": "deleted-message-1",
                            "subject": "OpenAI verification code",
                            "bodyPreview": "Your verification code is 654321",
                        }
                    ]
                },
            ),
        ]

        code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "654321")
        requested_urls = [str(call.args[1]) for call in mock_request.call_args_list]
        self.assertTrue(any("/me/mailFolders/deleteditems/messages" in url for url in requested_urls))

    @mock.patch("requests.post")
    def test_fetch_oauth_token_returns_empty_when_probe_gets_malformed_json_on_2xx(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.side_effect = [
            _FakeResponse(
                200,
                text="not-json",
                json_error=ValueError("malformed json"),
            ),
            _FakeResponse(
                200,
                text="still-not-json",
                json_error=ValueError("malformed json again"),
            ),
        ]

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "")
        attempted_scopes = [
            call.kwargs["data"].get("scope", "")
            for call in mock_post.call_args_list
        ]
        self.assertIn(
            "https://graph.microsoft.com/.default",
            attempted_scopes,
        )
        self.assertIn(
            "https://outlook.office.com/.default offline_access",
            attempted_scopes,
        )


if __name__ == "__main__":
    unittest.main()
