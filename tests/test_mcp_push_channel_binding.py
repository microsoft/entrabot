"""Push-pipeline contract tests for ActiveChannelBindings — confused-deputy authorization fix.

These tests verify that ``_push_channel_notification`` updates the
active-sponsor-channel binding store ONLY when ALL of these hold:

- the inbound sender is a configured Agent Identity sponsor (matched by
  Graph ``user_id``, not email),
- the message's ``sent_at`` parses to an epoch within the binding TTL,
- the channel push to ``write_stream`` succeeded (no transport error),
- the chat is a real Teams chat (not the synthetic ``email`` channel).

Any other condition must leave the binding store unchanged. This is the
fail-closed property the upcoming Gate 3 in ``add_member`` / ``share_file``
relies on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from entrabot.identity.active_channel import get_bindings, reset_for_tests


def _now_iso(offset_seconds: float = 0.0) -> str:
    """Return an ISO-8601 timestamp at now + offset_seconds, Z-suffixed."""
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat().replace(
        "+00:00", "Z"
    )


@pytest.fixture(autouse=True)
def _clean_bindings_and_state(tmp_path, monkeypatch):
    """Reset the binding singleton and detach any prior write_stream."""
    from entrabot import mcp_server

    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    reset_for_tests()
    mcp_server._state.pop("_write_stream", None)
    yield
    reset_for_tests()
    mcp_server._state.pop("_write_stream", None)


@pytest.fixture
def sponsor_record():
    """A fake sponsor whose user_id matches the test message sender_id."""
    s = MagicMock()
    s.user_id = "sponsor-uid-1"
    s.email_identifiers = lambda: ["sarah@example.com"]
    return s


class TestSponsorMessageBindsAfterSuccessfulPush:
    @pytest.mark.asyncio
    async def test_sponsor_inbound_creates_binding(
        self, monkeypatch, sponsor_record
    ) -> None:
        from entrabot import mcp_server

        mcp_server._state["_write_stream"] = AsyncMock()

        async def _sponsors():
            return [sponsor_record]

        monkeypatch.setattr(
            mcp_server, "_get_sponsor_records_for_binding", _sponsors
        )

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-bind-1",
                "from": "sarah@example.com",
                "sender_id": "sponsor-uid-1",
                "content": "hey",
                "sent_at": _now_iso(),
            },
            chat_id="19:realchat@thread.v2",
        )

        b = get_bindings().lookup("sponsor-uid-1")
        assert b is not None
        assert b.chat_id == "19:realchat@thread.v2"
        assert b.message_id == "m-bind-1"


class TestPushFailureDoesNotBind:
    @pytest.mark.asyncio
    async def test_send_raising_leaves_binding_unset(
        self, monkeypatch, sponsor_record
    ) -> None:
        from entrabot import mcp_server

        write_stream = AsyncMock()
        write_stream.send = AsyncMock(side_effect=RuntimeError("transport down"))
        mcp_server._state["_write_stream"] = write_stream

        async def _sponsors():
            return [sponsor_record]

        monkeypatch.setattr(
            mcp_server, "_get_sponsor_records_for_binding", _sponsors
        )

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-bind-2",
                "from": "sarah@example.com",
                "sender_id": "sponsor-uid-1",
                "content": "hey",
                "sent_at": _now_iso(),
            },
            chat_id="19:realchat@thread.v2",
        )

        assert get_bindings().lookup("sponsor-uid-1") is None

    @pytest.mark.asyncio
    async def test_no_write_stream_does_not_create_binding(
        self, monkeypatch, sponsor_record
    ) -> None:
        from entrabot import mcp_server

        # _write_stream intentionally absent (fixture already popped it)

        async def _sponsors():
            return [sponsor_record]

        monkeypatch.setattr(
            mcp_server, "_get_sponsor_records_for_binding", _sponsors
        )

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-bind-3",
                "from": "sarah@example.com",
                "sender_id": "sponsor-uid-1",
                "content": "hey",
                "sent_at": _now_iso(),
            },
            chat_id="19:realchat@thread.v2",
        )

        assert get_bindings().lookup("sponsor-uid-1") is None


class TestNonSponsorDoesNotBind:
    @pytest.mark.asyncio
    async def test_non_sponsor_sender_skips_binding(self, monkeypatch) -> None:
        from entrabot import mcp_server

        mcp_server._state["_write_stream"] = AsyncMock()

        async def _empty_sponsors():
            return []

        monkeypatch.setattr(
            mcp_server, "_get_sponsor_records_for_binding", _empty_sponsors
        )

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-rando",
                "from": "rando@example.com",
                "sender_id": "non-sponsor-uid",
                "content": "hi",
                "sent_at": _now_iso(),
            },
            chat_id="19:realchat@thread.v2",
        )

        assert get_bindings().snapshot() == {}


class TestEmailChannelDoesNotBind:
    @pytest.mark.asyncio
    async def test_email_chat_id_skips_binding(
        self, monkeypatch, sponsor_record
    ) -> None:
        from entrabot import mcp_server

        mcp_server._state["_write_stream"] = AsyncMock()

        async def _sponsors():
            return [sponsor_record]

        monkeypatch.setattr(
            mcp_server, "_get_sponsor_records_for_binding", _sponsors
        )

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-email",
                "from": "sarah@example.com",
                "sender_id": "sponsor-uid-1",
                "content": "subj",
                "sent_at": _now_iso(),
            },
            chat_id="email",
        )

        assert get_bindings().lookup("sponsor-uid-1") is None


class TestStaleSentAtDoesNotBind:
    @pytest.mark.asyncio
    async def test_old_message_skipped_per_ttl(
        self, monkeypatch, sponsor_record
    ) -> None:
        from entrabot import mcp_server

        mcp_server._state["_write_stream"] = AsyncMock()

        async def _sponsors():
            return [sponsor_record]

        monkeypatch.setattr(
            mcp_server, "_get_sponsor_records_for_binding", _sponsors
        )

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-stale",
                "from": "sarah@example.com",
                "sender_id": "sponsor-uid-1",
                "content": "ancient",
                "sent_at": "2000-01-01T00:00:00Z",
            },
            chat_id="19:realchat@thread.v2",
        )

        assert get_bindings().lookup("sponsor-uid-1") is None

    @pytest.mark.asyncio
    async def test_unparseable_sent_at_skipped(
        self, monkeypatch, sponsor_record
    ) -> None:
        from entrabot import mcp_server

        mcp_server._state["_write_stream"] = AsyncMock()

        async def _sponsors():
            return [sponsor_record]

        monkeypatch.setattr(
            mcp_server, "_get_sponsor_records_for_binding", _sponsors
        )

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-bad-ts",
                "from": "sarah@example.com",
                "sender_id": "sponsor-uid-1",
                "content": "hey",
                "sent_at": "not-a-date",
            },
            chat_id="19:realchat@thread.v2",
        )

        assert get_bindings().lookup("sponsor-uid-1") is None

    @pytest.mark.asyncio
    async def test_missing_sender_id_skips_binding(
        self, monkeypatch, sponsor_record
    ) -> None:
        """Inbound message with no Graph sender_id cannot key a binding."""
        from entrabot import mcp_server

        mcp_server._state["_write_stream"] = AsyncMock()

        async def _sponsors():
            return [sponsor_record]

        monkeypatch.setattr(
            mcp_server, "_get_sponsor_records_for_binding", _sponsors
        )

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-no-sender-id",
                "from": "sarah@example.com",
                # sender_id deliberately absent
                "content": "hey",
                "sent_at": _now_iso(),
            },
            chat_id="19:realchat@thread.v2",
        )

        assert get_bindings().snapshot() == {}
