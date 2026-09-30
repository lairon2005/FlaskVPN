# tests/test_mail.py
"""Выбор транспорта в webapp/core/mail.py: Resend HTTP API при наличии ключа, иначе SMTP."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from webapp.core import mail


def _run(coro):
    return asyncio.run(coro)


def _client_with(handler):
    real = httpx.AsyncClient
    return lambda **kw: real(transport=httpx.MockTransport(handler), **kw)


class ResendTransportTest(unittest.TestCase):
    def test_sends_json_with_bearer_key(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"id": "abc"})

        with patch.object(mail, "RESEND_API_KEY", "re_test"), \
                patch.object(mail.httpx, "AsyncClient", _client_with(handler)):
            _run(mail.send_verification_email("user@example.com", "123456"))

        self.assertEqual(seen["url"], mail.RESEND_API_URL)
        self.assertEqual(seen["auth"], "Bearer re_test")
        self.assertEqual(seen["body"]["to"], ["user@example.com"])
        self.assertIn("123456", seen["body"]["html"])
        self.assertIn(mail.MAIL_FROM, seen["body"]["from"])

    def test_api_error_becomes_safe_mail_send_error(self):
        handler = lambda request: httpx.Response(403, json={"message": "domain is not verified"})
        with patch.object(mail, "RESEND_API_KEY", "re_test"), \
                patch.object(mail.httpx, "AsyncClient", _client_with(handler)):
            with self.assertRaises(mail.MailSendError) as ctx:
                _run(mail.send_reset_code("user@example.com", "123456"))
        self.assertNotIn("domain", str(ctx.exception))
        self.assertNotIn("re_test", str(ctx.exception))

    def test_timeout_becomes_mail_send_error(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with patch.object(mail, "RESEND_API_KEY", "re_test"), \
                patch.object(mail.httpx, "AsyncClient", _client_with(handler)):
            with self.assertRaises(mail.MailSendError):
                _run(mail.send_reset_code("user@example.com", "123456"))


class SmtpFallbackTest(unittest.TestCase):
    def test_without_key_uses_smtp(self):
        with patch.object(mail, "RESEND_API_KEY", ""), \
                patch.object(mail, "_send_via_smtp", AsyncMock()) as smtp, \
                patch.object(mail, "_send_via_resend", AsyncMock()) as resend:
            _run(mail.send_reset_code("user@example.com", "123456"))
        smtp.assert_awaited_once()
        resend.assert_not_awaited()

    def test_smtp_failure_becomes_mail_send_error(self):
        with patch.object(mail, "RESEND_API_KEY", ""), \
                patch.object(mail, "_send_via_smtp", AsyncMock(side_effect=OSError("refused"))):
            with self.assertRaises(mail.MailSendError):
                _run(mail.send_reset_code("user@example.com", "123456"))


if __name__ == "__main__":
    unittest.main()
