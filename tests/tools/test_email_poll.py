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

from entraclaw.tools.email_poll import (
    GRAPH_MESSAGES_URL,
    is_substantive,
    load_cursor,
    poll_once,
    save_cursor,
)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))
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

    def test_keeps_real_microsoft_sender(self) -> None:
        assert is_substantive("user@example.com")
        assert is_substantive("diana.smetters@microsoft.com")

    def test_keeps_external_domain(self) -> None:
        assert is_substantive("partner@contoso.com")

    def test_handles_empty_or_none(self) -> None:
        # Empty addresses are treated as non-substantive (nothing to surface).
        assert not is_substantive("")
        assert not is_substantive(None)  # type: ignore[arg-type]

    def test_case_insensitive(self) -> None:
        assert not is_substantive("NO-REPLY@Teams.Mail.Microsoft")


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
        "toRecipients": [{"emailAddress": {"address": "entraclaw-agent@fabrikam.onmicrosoft.com"}}],
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
                sender="diana.smetters@microsoft.com",
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
        assert new_cursor == "2026-04-16T19:07:00Z"

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
        assert new_cursor == "2026-04-16T19:09:00Z"

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
            sender="boss@microsoft.com",
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
            sender="boss@microsoft.com",
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
        from entraclaw.errors import TokenExpiredError

        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(return_value=httpx.Response(401))
            with pytest.raises(TokenExpiredError):
                await poll_once(token="tok", cursor=None)
