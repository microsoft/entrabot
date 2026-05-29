"""Tests for email_poll — per-minute Graph poll of /me/messages.

Polls the agent's mailbox, filters out known noise (Teams
notifications, M365 marketing), detects Purview-encrypted mail, and
returns the substantive messages for the MCP server to push as
channel notifications + write to the interaction log.

Cursor is a single RFC 3339 timestamp persisted at
``<config.data_dir>/email_cursor.txt``. On first run, the cursor is
absent and the poller initializes to "now" so we don't flood the
agent with historical mail.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from entrabot.tools.email_poll import (
    GRAPH_MESSAGES_URL,
    advance_cursor,
    is_substantive,
    load_cursor,
    poll_once,
    save_cursor,
)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    return tmp_path


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
# cursor persistence
# ---------------------------------------------------------------------------
class TestCursor:
    def test_load_returns_none_when_missing(self, tmp_data_dir: Path) -> None:
        assert load_cursor() is None

    def test_save_then_load_roundtrip(self, tmp_data_dir: Path) -> None:
        ts = "2026-04-16T19:00:00Z"
        save_cursor(ts)
        assert load_cursor() == ts

    def test_save_overwrites(self, tmp_data_dir: Path) -> None:
        save_cursor("2026-04-16T19:00:00Z")
        save_cursor("2026-04-16T20:00:00Z")
        assert load_cursor() == "2026-04-16T20:00:00Z"

    def test_save_strips_whitespace(self, tmp_data_dir: Path) -> None:
        save_cursor("  2026-04-16T19:00:00Z  \n")
        assert load_cursor() == "2026-04-16T19:00:00Z"


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
