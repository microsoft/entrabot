"""EntraClaw Teams bot server — M365 Agents SDK + aiohttp.

Thin bot server that runs alongside the MCP server. Receives inbound
Teams messages via Azure Bot Service → Dev Tunnel → localhost:3978,
writes them to ``~/.entraclaw/bot/inbound.jsonl`` for the MCP server
to consume. Reads ``~/.entraclaw/bot/outbound.jsonl`` for proactive
messages to send back to Teams.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from entraclaw.bot.convo_store import load_all_references, save_reference
from entraclaw.bot.handler import read_outbound, write_inbound

logger = logging.getLogger("entraclaw.bot.server")


class EntraClawBot:
    """Activity handler for EntraClaw Teams bot.

    Receives inbound messages from Teams and writes them to the
    shared JSONL file. Saves conversation references for proactive
    messaging.
    """

    async def on_message(self, context: Any) -> None:
        """Handle incoming message from Teams user.

        Writes the message to inbound.jsonl for the MCP server to pick up.
        Skips activities with no text (images, card actions, typing).
        """
        activity = context.activity
        if not activity.text:
            logger.debug("Skipping activity with no text (id=%s)", activity.id)
            return

        message = {
            "message_id": activity.id,
            "from": getattr(activity.from_property, "name", "unknown"),
            "from_id": getattr(activity.from_property, "aad_object_id", None)
            or getattr(activity.from_property, "id", "unknown"),
            "content": activity.text,
            "sent_at": str(activity.timestamp) if activity.timestamp else "",
            "conversation_id": activity.conversation.id,
        }

        write_inbound(message)
        logger.info(
            "Inbound message from %s: %s",
            message["from"],
            message["content"][:50],
        )

        # Save/update conversation reference for proactive messaging
        _save_convo_ref(activity)

    async def on_members_added(
        self, members_added: list, context: Any
    ) -> None:
        """Handle bot installation — save conversation reference."""
        activity = context.activity
        bot_id = activity.recipient.id if activity.recipient else None

        for member in members_added:
            if member.id == bot_id:
                _save_convo_ref(activity)
                logger.info(
                    "Bot installed in conversation %s", activity.conversation.id
                )

    async def on_error(self, context: Any, error: Exception) -> None:
        """Log unhandled errors from the bot adapter."""
        logger.error("Bot error: %s", error)


def _save_convo_ref(activity: Any) -> None:
    """Extract and persist a conversation reference from an activity."""
    ref = {
        "conversation_id": activity.conversation.id,
        "service_url": getattr(activity, "service_url", None),
        "channel_id": getattr(activity, "channel_id", None),
        "bot_id": getattr(activity.recipient, "id", None) if activity.recipient else None,
        "bot_name": getattr(activity.recipient, "name", None) if activity.recipient else None,
    }
    save_reference(activity.conversation.id, ref)


# ── Outbound message pump ──────────────────────────────────────────

OUTBOUND_POLL_INTERVAL = 2  # seconds


async def _outbound_pump(adapter: Any, bot: EntraClawBot) -> None:
    """Poll outbound.jsonl and send messages via the bot adapter.

    Reads conversation references from the store to route messages
    to the correct Teams conversation.
    """
    while True:
        try:
            messages = read_outbound()
            if messages:
                refs = load_all_references()
                for msg in messages:
                    chat_id = msg.get("chat_id")
                    content = msg.get("content", "")
                    if not content:
                        continue

                    # Find the conversation reference
                    ref = None
                    if chat_id and chat_id in refs:
                        ref = refs[chat_id]
                    elif refs:
                        # Default to first available conversation
                        ref = next(iter(refs.values()))

                    if ref:
                        logger.info("Sending proactive message: %s", content[:50])
                        # Proactive send will be wired to adapter.continue_conversation
                        # once we have a working adapter instance
                    else:
                        logger.warning(
                            "No conversation reference available — cannot send: %s",
                            content[:50],
                        )
        except Exception as exc:
            logger.error("Outbound pump error: %s", exc)

        await asyncio.sleep(OUTBOUND_POLL_INTERVAL)


# ── aiohttp app factory ───────────────────────────────────────────


def create_bot_app() -> web.Application:
    """Create the aiohttp web application for the bot server.

    Registers the ``/api/messages`` endpoint that Azure Bot Service
    sends activities to.
    """
    app = web.Application()

    async def handle_messages(request: web.Request) -> web.Response:
        """Receive activities from Azure Bot Service."""
        # In full integration, this delegates to the CloudAdapter
        # For now, parse the activity and route to the bot handler
        try:
            body = await request.json()
            logger.info("Received activity type=%s", body.get("type", "unknown"))
            return web.Response(status=200)
        except Exception as exc:
            logger.error("Failed to process activity: %s", exc)
            return web.Response(status=500, text=str(exc))

    app.router.add_post("/api/messages", handle_messages)
    return app


async def run_bot_server(port: int = 3978) -> None:
    """Start the bot server on the given port.

    Launches the aiohttp server and the outbound message pump.
    """
    bot = EntraClawBot()
    app = create_bot_app()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()
    logger.info("Bot server listening on http://localhost:%d/api/messages", port)

    # Start outbound pump (proactive messaging)
    asyncio.create_task(_outbound_pump(None, bot))

    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    import os

    logging.basicConfig(level=os.environ.get("ENTRACLAW_LOG_LEVEL", "INFO"))
    port = int(os.environ.get("ENTRACLAW_BOT_TUNNEL_PORT", "3978"))
    asyncio.run(run_bot_server(port))
