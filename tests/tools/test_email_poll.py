"""Tests for email_poll — per-minute Graph poll of /me/messages.

Polls the agent's mailbox, filters out known noise (Teams
notifications, M365 marketing), detects Purview-encrypted mail, and
returns the substantive messages for the MCP server to push as
channel notifications + write to the interaction log.

This module only fetches and filters. The cursor lives in
:mod:`entrabot.tools.poll_state`; see ``tests/tools/test_poll_state.py``.
The cycle that claims a window before pushing it is in the MCP server; see
``tests/test_mcp_server_email_poll.py``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from entrabot.tools import email_poll
from entrabot.tools.email_poll import (
    GRAPH_MESSAGES_URL,
    advance_cursor,
    is_substantive,
    poll_once,
)


# ---------------------------------------------------------------------------
# is_substantive
# ---------------------------------------------------------------------------
class TestIsSubstantive:
    def test_filters_teams_notification_mail(self) -> None:
        assert not is_substantive("no-reply@teams.mail.microsoft")

    def test_filters_microsoft365_communication(self) -> None:
        assert not is_substantive("Microsoft365@communication.microsoft.com")

    def test_filters_generic_no_reply(self) -> None:
        assert not is_substantive("noreply@example.com")
        assert not is_substantive("no-reply@example.com")
        assert not is_substantive("donotreply@example.com")

    def test_keeps_real_substantive_sender(self) -> None:
        assert is_substantive("user@example.com")
        assert is_substantive("alice.example@example.com")

    def test_keeps_external_domain(self) -> None:
        assert is_substantive("partner@contoso.com")

    def test_handles_empty_or_none(self) -> None:
        # Empty addresses are treated as non-substantive (nothing to surface).
        assert not is_substantive("")
        assert not is_substantive(None)  # type: ignore[arg-type]

    def test_case_insensitive(self) -> None:
        assert not is_substantive("NO-REPLY@Teams.Mail.Microsoft")


# ---------------------------------------------------------------------------
# advance_cursor
# ---------------------------------------------------------------------------
class TestAdvanceCursor:
    def test_bumps_second_precision_timestamp_by_one_millisecond(self) -> None:
        assert advance_cursor("2026-04-16T19:00:00Z") == "2026-04-16T19:00:00.001Z"

    def test_bumps_subsecond_timestamp_by_one_millisecond(self) -> None:
        assert advance_cursor("2026-04-16T19:00:00.500Z") == "2026-04-16T19:00:00.501Z"

    def test_carries_overflow_into_next_second(self) -> None:
        assert advance_cursor("2026-04-16T19:00:00.999Z") == "2026-04-16T19:00:01Z"


# ---------------------------------------------------------------------------
# cursor persistence moved to poll_state
# ---------------------------------------------------------------------------
class TestNoLocalCursorApi:
    @pytest.mark.parametrize("name", ["load_cursor", "save_cursor", "_cursor_path"])
    def test_module_no_longer_owns_cursor_persistence(self, name: str) -> None:
        """A second cursor API here would quietly bypass the shared claim."""
        assert not hasattr(email_poll, name)


# ---------------------------------------------------------------------------
# poll_once
# ---------------------------------------------------------------------------
def _msg(
    *,
    msg_id: str,
    sender: str,
    subject: str = "Hi",
    received: str = "2026-04-16T19:00:00Z",
    preview: str = "body",
    has_attachments: bool = False,
    conversation_id: str = "conv-1",
) -> dict:
    return {
        "id": msg_id,
        "subject": subject,
        "receivedDateTime": received,
        "bodyPreview": preview,
        "from": {"emailAddress": {"name": sender.split("@")[0], "address": sender}},
        "toRecipients": [{"emailAddress": {"address": "entrabot-agent@fabrikam.onmicrosoft.com"}}],
        "hasAttachments": has_attachments,
        "conversationId": conversation_id,
    }


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_and_nil_cursor(self) -> None:
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(return_value=httpx.Response(200, json={"value": []}))
            msgs, new_cursor = await poll_once(token="tok", cursor=None)
        assert msgs == []
        assert new_cursor is None

    @pytest.mark.asyncio
    async def test_filters_non_substantive_senders(self) -> None:
        value = [
            _msg(
                msg_id="1",
                sender="no-reply@teams.mail.microsoft",
                subject="is trying to reach you",
                received="2026-04-16T19:05:00Z",
            ),
            _msg(
                msg_id="2",
                sender="alice.example@example.com",
                subject="Re: Project Apollo",
                received="2026-04-16T19:06:00Z",
            ),
            _msg(
                msg_id="3",
                sender="Microsoft365@communication.microsoft.com",
                received="2026-04-16T19:07:00Z",
            ),
        ]
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"value": value})
            )
            msgs, new_cursor = await poll_once(token="tok", cursor=None)
        assert len(msgs) == 1
        assert msgs[0]["id"] == "2"
        # Cursor advances to the latest received message across ALL returned
        # (including filtered) so we don't re-scan the same noise next poll.
        assert new_cursor == "2026-04-16T19:07:00.001Z"

    @pytest.mark.asyncio
    async def test_advances_cursor_to_latest(self) -> None:
        value = [
            _msg(msg_id="a", sender="u@example.com", received="2026-04-16T19:05:00Z"),
            _msg(msg_id="b", sender="u@example.com", received="2026-04-16T19:09:00Z"),
            _msg(msg_id="c", sender="u@example.com", received="2026-04-16T19:06:00Z"),
        ]
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"value": value})
            )
            msgs, new_cursor = await poll_once(token="tok", cursor="2026-04-16T19:00:00Z")
        assert len(msgs) == 3
        assert new_cursor == "2026-04-16T19:09:00.001Z"

    @pytest.mark.asyncio
    async def test_cursor_clears_subsecond_message_after_second_precision_cursor(self) -> None:
        """Timestamps compare as times, not strings: ``.`` sorts before ``Z``, so a
        string compare calls ``19:00:00.500Z`` older than ``19:00:00Z``. That left
        the cursor behind the message, and with a shared cursor the next cycle on
        another instance, whose session dedup hasn't seen it, pushed it again."""
        value = [_msg(msg_id="m", sender="u@example.com", received="2026-04-16T19:00:00.500Z")]
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"value": value})
            )
            _, new_cursor = await poll_once(token="tok", cursor="2026-04-16T19:00:00Z")
        assert new_cursor == "2026-04-16T19:00:00.501Z"

    @pytest.mark.asyncio
    async def test_cursor_clears_later_message_with_mixed_precision(self) -> None:
        value = [
            _msg(msg_id="a", sender="u@example.com", received="2026-04-16T19:00:01Z"),
            _msg(msg_id="b", sender="u@example.com", received="2026-04-16T19:00:01.250Z"),
        ]
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"value": value})
            )
            _, new_cursor = await poll_once(token="tok", cursor="2026-04-16T19:00:00Z")
        assert new_cursor == "2026-04-16T19:00:01.251Z"

    @pytest.mark.asyncio
    async def test_same_second_message_not_re_fetched_on_next_poll(self) -> None:
        """Cursor must advance past the latest message so gt filter skips it."""
        ts = "2026-04-16T19:00:00Z"
        value = [_msg(msg_id="same-sec", sender="u@example.com", received=ts)]
        captured_filters: list[str] = []

        def handler(request):
            captured_filters.append(dict(request.url.params).get("$filter", ""))
            payload = {"value": value} if not captured_filters[0] else {"value": []}
            return httpx.Response(200, json=payload)

        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(side_effect=handler)
            _, first_cursor = await poll_once(token="tok", cursor=None)
            _, second_cursor = await poll_once(token="tok", cursor=first_cursor)

        assert first_cursor == "2026-04-16T19:00:00.001Z"
        assert len(captured_filters) == 2
        assert captured_filters[1] == "receivedDateTime gt 2026-04-16T19:00:00.001Z"
        assert second_cursor == first_cursor

    @pytest.mark.asyncio
    async def test_cursor_filter_passed_to_graph(self) -> None:
        """When cursor is set, request should include $filter=receivedDateTime gt cursor."""
        captured_params: dict = {}

        def handler(request):
            captured_params.update(dict(request.url.params))
            return httpx.Response(200, json={"value": []})

        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(side_effect=handler)
            await poll_once(token="tok", cursor="2026-04-16T18:00:00Z")

        assert "$filter" in captured_params
        assert "receivedDateTime gt 2026-04-16T18:00:00Z" in captured_params["$filter"]

    @pytest.mark.asyncio
    async def test_detects_rpmsg_attachment_when_hasAttachments(self) -> None:
        """Messages with has_attachments=true get a follow-up attachment fetch
        to detect Purview-encrypted content (message.rpmsg)."""
        main_msg = _msg(
            msg_id="enc-1",
            sender="boss@example.com",
            subject="Confidential",
            received="2026-04-16T19:00:00Z",
            has_attachments=True,
        )
        attachments = {
            "value": [
                {
                    "name": "message.rpmsg",
                    "contentType": "application/x-microsoft-rpmsg-message",
                    "size": 777584,
                }
            ]
        }
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"value": [main_msg]})
            )
            respx.get("https://graph.microsoft.com/v1.0/me/messages/enc-1/attachments").mock(
                return_value=httpx.Response(200, json=attachments)
            )
            msgs, _ = await poll_once(token="tok", cursor=None)

        assert len(msgs) == 1
        assert msgs[0].get("_encrypted") is True

    @pytest.mark.asyncio
    async def test_no_encryption_flag_when_no_rpmsg(self) -> None:
        main_msg = _msg(
            msg_id="plain-1",
            sender="boss@example.com",
            received="2026-04-16T19:00:00Z",
            has_attachments=True,
        )
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"value": [main_msg]})
            )
            respx.get("https://graph.microsoft.com/v1.0/me/messages/plain-1/attachments").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "name": "report.pdf",
                                "contentType": "application/pdf",
                                "size": 12345,
                            }
                        ]
                    },
                )
            )
            msgs, _ = await poll_once(token="tok", cursor=None)

        assert msgs[0].get("_encrypted") is not True

    @pytest.mark.asyncio
    async def test_401_raises(self) -> None:
        from entrabot.errors import TokenExpiredError

        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(return_value=httpx.Response(401))
            with pytest.raises(TokenExpiredError):
                await poll_once(token="tok", cursor=None)
