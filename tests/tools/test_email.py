"""Tests for entrabot.tools.email.send_email.

The helper wraps Graph's ``/me/sendMail`` (new mail) and
``/me/messages/{id}/reply`` (threaded reply), returns a dict with
``sent_at`` on success, and maps errors onto the typed hierarchy used
by the Teams helpers (TokenExpiredError on 401, RateLimitError on 429,
EmailSendError otherwise).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

GRAPH_SENDMAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
GRAPH_REPLY_URL_TEMPLATE = "https://graph.microsoft.com/v1.0/me/messages/{message_id}/reply"


class TestSendEmailSingleRecipient:
    @pytest.mark.asyncio
    async def test_posts_to_sendmail_with_correct_shape(self) -> None:
        from entrabot.tools.email import send_email

        captured: dict = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.read())
            captured["headers"] = dict(request.headers)
            return httpx.Response(202)

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(side_effect=handler)
            result = await send_email(
                to=["alice@example.com"],
                subject="Re: status",
                body="<p>hi</p>",
                token="tok",
            )

        assert "sendMail" in captured["url"]
        assert captured["headers"]["authorization"] == "Bearer tok"

        msg = captured["json"]["message"]
        assert msg["subject"] == "Re: status"
        assert msg["body"]["contentType"] == "HTML"
        assert msg["body"]["content"] == "<p>hi</p>"
        assert msg["toRecipients"] == [{"emailAddress": {"address": "alice@example.com"}}]
        assert captured["json"]["saveToSentItems"] is True
        assert "sent_at" in result


class TestSendEmailMultipleRecipients:
    @pytest.mark.asyncio
    async def test_posts_with_to_cc_bcc(self) -> None:
        from entrabot.tools.email import send_email

        captured: dict = {}

        def handler(request):
            captured["json"] = json.loads(request.read())
            return httpx.Response(202)

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(side_effect=handler)
            await send_email(
                to=["a@example.com", "b@example.com"],
                subject="hi",
                body="<p>body</p>",
                cc=["c@example.com"],
                bcc=["d@example.com"],
                token="tok",
            )

        msg = captured["json"]["message"]
        assert msg["toRecipients"] == [
            {"emailAddress": {"address": "a@example.com"}},
            {"emailAddress": {"address": "b@example.com"}},
        ]
        assert msg["ccRecipients"] == [{"emailAddress": {"address": "c@example.com"}}]
        assert msg["bccRecipients"] == [{"emailAddress": {"address": "d@example.com"}}]


class TestSendEmailReply:
    @pytest.mark.asyncio
    async def test_reply_posts_to_reply_endpoint(self) -> None:
        from entrabot.tools.email import send_email

        message_id = "AAMkADf0=="
        reply_url = GRAPH_REPLY_URL_TEMPLATE.format(message_id=message_id)

        captured: dict = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.read())
            return httpx.Response(202)

        with respx.mock:
            respx.post(reply_url).mock(side_effect=handler)
            result = await send_email(
                to=["alice@example.com"],
                subject="ignored-by-reply",
                body="<p>thanks</p>",
                reply_to_message_id=message_id,
                token="tok",
            )

        # Reply endpoint, not sendMail
        assert message_id in captured["url"]
        assert "sendMail" not in captured["url"]
        assert captured["url"].endswith("/reply")

        # Graph's reply payload nests a ``message`` object + optional comment.
        msg = captured["json"]["message"]
        assert msg["body"]["contentType"] == "HTML"
        assert msg["body"]["content"] == "<p>thanks</p>"
        assert msg["toRecipients"] == [{"emailAddress": {"address": "alice@example.com"}}]
        assert "sent_at" in result


class TestSendEmailStatusCodes:
    @pytest.mark.asyncio
    async def test_returns_sent_at_on_202(self) -> None:
        from entrabot.tools.email import send_email

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(return_value=httpx.Response(202))
            result = await send_email(
                to=["a@example.com"],
                subject="s",
                body="b",
                token="tok",
            )
        assert "sent_at" in result
        assert isinstance(result["sent_at"], str)

    @pytest.mark.asyncio
    async def test_401_raises_token_expired(self) -> None:
        from entrabot.errors import TokenExpiredError
        from entrabot.tools.email import send_email

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(return_value=httpx.Response(401))
            with pytest.raises(TokenExpiredError):
                await send_email(
                    to=["a@example.com"],
                    subject="s",
                    body="b",
                    token="tok",
                )

    @pytest.mark.asyncio
    async def test_429_raises_rate_limit(self) -> None:
        from entrabot.errors import RateLimitError
        from entrabot.tools.email import send_email

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "15"})
            )
            with pytest.raises(RateLimitError) as exc_info:
                await send_email(
                    to=["a@example.com"],
                    subject="s",
                    body="b",
                    token="tok",
                )
            assert exc_info.value.retry_after == 15

    @pytest.mark.asyncio
    async def test_429_http_date_retry_after_raises_rate_limit(self) -> None:
        from entrabot.errors import RateLimitError
        from entrabot.tools.email import send_email

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(
                return_value=httpx.Response(
                    429,
                    headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
                )
            )
            with pytest.raises(RateLimitError) as exc_info:
                await send_email(
                    to=["a@example.com"],
                    subject="s",
                    body="b",
                    token="tok",
                )
            assert exc_info.value.retry_after >= 0

    @pytest.mark.asyncio
    async def test_400_raises_email_send_error_with_graph_body(self) -> None:
        from entrabot.tools.email import EmailSendError, send_email

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": "ErrorInvalidRecipients",
                            "message": "Recipient not found",
                        }
                    },
                )
            )
            with pytest.raises(EmailSendError) as exc_info:
                await send_email(
                    to=["bogus"],
                    subject="s",
                    body="b",
                    token="tok",
                )
            # Graph error body reaches the exception for operator debugging.
            assert "ErrorInvalidRecipients" in str(exc_info.value) or (
                "Recipient not found" in str(exc_info.value)
            )

    @pytest.mark.asyncio
    async def test_500_raises_email_send_error(self) -> None:
        from entrabot.tools.email import EmailSendError, send_email

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(return_value=httpx.Response(500, text="boom"))
            with pytest.raises(EmailSendError):
                await send_email(
                    to=["a@example.com"],
                    subject="s",
                    body="b",
                    token="tok",
                )


class TestSendEmailContentType:
    @pytest.mark.asyncio
    async def test_html_content_type_sent_as_html_capitalized(self) -> None:
        from entrabot.tools.email import send_email

        captured: dict = {}

        def handler(request):
            captured["json"] = json.loads(request.read())
            return httpx.Response(202)

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(side_effect=handler)
            await send_email(
                to=["a@example.com"],
                subject="s",
                body="b",
                content_type="HTML",
                token="tok",
            )
        assert captured["json"]["message"]["body"]["contentType"] == "HTML"

    @pytest.mark.asyncio
    async def test_text_content_type_sent_as_text_capitalized(self) -> None:
        from entrabot.tools.email import send_email

        captured: dict = {}

        def handler(request):
            captured["json"] = json.loads(request.read())
            return httpx.Response(202)

        with respx.mock:
            respx.post(GRAPH_SENDMAIL_URL).mock(side_effect=handler)
            await send_email(
                to=["a@example.com"],
                subject="s",
                body="plain",
                content_type="Text",
                token="tok",
            )
        assert captured["json"]["message"]["body"]["contentType"] == "Text"
