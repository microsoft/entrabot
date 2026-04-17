"""Tests for bot server module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botbuilder.core import TurnContext
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ChannelAccount,
    ConversationAccount,
)

from entraclaw.bot.server import (
    EntraClawBot,
    _convo_ref_to_dict,
    _dict_to_convo_ref,
    create_bot_app,
)


def _make_activity(
    text: str | None = "hello from teams",
    activity_id: str = "msg-123",
    from_name: str = "Brandon",
    from_id: str = "user-456",
    from_aad_id: str = "aad-789",
    conversation_id: str = "conv-abc",
) -> Activity:
    """Build a realistic Activity object for testing."""
    return Activity(
        type=ActivityTypes.message,
        id=activity_id,
        text=text,
        timestamp="2026-04-10T20:00:00Z",
        from_property=ChannelAccount(
            id=from_id, name=from_name, aad_object_id=from_aad_id
        ),
        recipient=ChannelAccount(id="bot-id", name="EntraClaw Bot"),
        conversation=ConversationAccount(id=conversation_id),
        channel_id="msteams",
        service_url="https://smba.trafficmanager.net/amer/",
    )


class TestEntraClawBot:
    """Tests for the EntraClaw bot activity handler."""

    @pytest.fixture
    def bot(self) -> EntraClawBot:
        return EntraClawBot()

    def test_bot_creation(self, bot: EntraClawBot) -> None:
        assert bot is not None

    @pytest.mark.asyncio
    async def test_on_message_writes_inbound(self, bot: EntraClawBot) -> None:
        """When bot receives a message, it writes to inbound.jsonl."""
        activity = _make_activity(text="hello from teams")
        turn_context = MagicMock(spec=TurnContext)
        turn_context.activity = activity

        with (
            patch("entraclaw.bot.server.write_inbound") as mock_write,
            patch("entraclaw.bot.server.save_reference"),
        ):
            await bot.on_message_activity(turn_context)

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert written["content"] == "hello from teams"
        assert written["from"] == "Brandon"
        assert written["message_id"] == "msg-123"
        assert written["conversation_id"] == "conv-abc"

    @pytest.mark.asyncio
    async def test_on_message_skips_empty_text(self, bot: EntraClawBot) -> None:
        """Messages with no text (images, cards) are skipped."""
        activity = _make_activity(text=None)
        turn_context = MagicMock(spec=TurnContext)
        turn_context.activity = activity

        with patch("entraclaw.bot.server.write_inbound") as mock_write:
            await bot.on_message_activity(turn_context)

        mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_members_added_saves_convo_ref(self, bot: EntraClawBot) -> None:
        """When bot is installed, conversation reference is saved."""
        activity = _make_activity()
        turn_context = MagicMock(spec=TurnContext)
        turn_context.activity = activity

        bot_member = ChannelAccount(id="bot-id")

        with patch("entraclaw.bot.server.save_reference") as mock_save:
            await bot.on_members_added_activity(
                [bot_member],
                turn_context,
            )

        mock_save.assert_called_once()


class TestConvoRefSerialization:
    """Tests for ConversationReference dict round-trip."""

    def test_round_trip(self) -> None:
        """ConversationReference → dict → ConversationReference preserves data."""
        from botbuilder.schema import ConversationReference

        ref = ConversationReference(
            service_url="https://smba.trafficmanager.net/amer/",
            channel_id="msteams",
            activity_id="act-1",
            conversation=ConversationAccount(id="conv-123", name="Test Chat"),
            bot=ChannelAccount(id="bot-1", name="EntraClaw Bot"),
            user=ChannelAccount(id="user-1", name="Brandon"),
        )
        d = _convo_ref_to_dict(ref)
        restored = _dict_to_convo_ref(d)

        assert restored.service_url == ref.service_url
        assert restored.channel_id == ref.channel_id
        assert restored.conversation.id == ref.conversation.id
        assert restored.bot.id == ref.bot.id
        assert restored.user.id == ref.user.id


class TestCreateBotApp:
    """Tests for the aiohttp app factory."""

    def test_creates_app_with_routes(self) -> None:
        adapter = MagicMock()
        bot = EntraClawBot()
        app = create_bot_app(adapter, bot)
        routes = [
            r.resource.canonical
            for r in app.router.routes()
            if hasattr(r, "resource") and r.resource
        ]
        assert "/api/messages" in routes
        assert "/health" in routes
