"""EntraBot Teams bot server — Bot Framework SDK + aiohttp.

Thin bot server that runs alongside the MCP server. Receives inbound
Teams messages via Azure Bot Service → Dev Tunnel → localhost:3978,
writes them to ``~/.entrabot/bot/inbound.jsonl`` for the MCP server
to consume. Reads ``~/.entrabot/bot/outbound.jsonl`` for proactive
messages to send back to Teams.

Uses botbuilder-core (4.17.x) — the stable Bot Framework SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from aiohttp import web
from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.core.bot_framework_adapter import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
)
from botbuilder.schema import Activity, ConversationReference

from entrabot.bot.convo_store import load_all_references, save_reference
from entrabot.bot.handler import read_outbound, write_inbound

logger = logging.getLogger("entrabot.bot.server")


class EntraBotBot(ActivityHandler):
    """Activity handler for EntraBot Teams bot.

    Receives inbound messages from Teams and writes them to the
    shared JSONL file. Saves conversation references for proactive
    messaging.
    """

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        """Handle incoming message from Teams user."""
        activity = turn_context.activity
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

        # Save conversation reference for proactive messaging
        ref = TurnContext.get_conversation_reference(activity)
        _save_convo_ref(ref)

    async def on_members_added_activity(
        self, members_added: list, turn_context: TurnContext
    ) -> None:
        """Handle bot installation — save conversation reference."""
        activity = turn_context.activity
        bot_id = activity.recipient.id if activity.recipient else None

        for member in members_added:
            if member.id == bot_id:
                ref = TurnContext.get_conversation_reference(activity)
                _save_convo_ref(ref)
                logger.info("Bot installed in conversation %s", activity.conversation.id)

    async def on_turn(self, turn_context: TurnContext) -> None:
        """Called on every turn — save convo ref, then dispatch."""
        activity = turn_context.activity
        if activity.conversation:
            ref = TurnContext.get_conversation_reference(activity)
            _save_convo_ref(ref)
        await super().on_turn(turn_context)


def _save_convo_ref(ref: ConversationReference) -> None:
    """Persist a Bot Framework ConversationReference as JSON."""
    convo_id = ref.conversation.id if ref.conversation else None
    if not convo_id:
        return
    # Serialize the ConversationReference to a plain dict
    ref_dict = _convo_ref_to_dict(ref)
    save_reference(convo_id, ref_dict)


def _convo_ref_to_dict(ref: ConversationReference) -> dict:
    """Convert a ConversationReference to a serializable dict."""
    return {
        "conversation_id": ref.conversation.id if ref.conversation else None,
        "service_url": ref.service_url,
        "channel_id": ref.channel_id,
        "bot_id": ref.bot.id if ref.bot else None,
        "bot_name": ref.bot.name if ref.bot else None,
        "user_id": ref.user.id if ref.user else None,
        "user_name": ref.user.name if ref.user else None,
        "activity_id": ref.activity_id,
        "conversation_is_group": getattr(ref.conversation, "is_group", None),
        "conversation_name": getattr(ref.conversation, "name", None),
    }


def _dict_to_convo_ref(d: dict) -> ConversationReference:
    """Rebuild a ConversationReference from a stored dict."""
    from botbuilder.schema import ChannelAccount, ConversationAccount

    ref = ConversationReference(
        service_url=d.get("service_url"),
        channel_id=d.get("channel_id"),
        activity_id=d.get("activity_id"),
    )
    if d.get("conversation_id"):
        ref.conversation = ConversationAccount(
            id=d["conversation_id"],
            is_group=d.get("conversation_is_group"),
            name=d.get("conversation_name"),
        )
    if d.get("bot_id"):
        ref.bot = ChannelAccount(id=d["bot_id"], name=d.get("bot_name"))
    if d.get("user_id"):
        ref.user = ChannelAccount(id=d["user_id"], name=d.get("user_name"))
    return ref


# ── Outbound message pump ──────────────────────────────────────────

OUTBOUND_POLL_INTERVAL = 2  # seconds


async def _outbound_pump(adapter: BotFrameworkAdapter, bot: EntraBotBot) -> None:
    """Poll outbound.jsonl and send proactive messages via the adapter."""
    while True:
        try:
            messages = read_outbound()
            if messages:
                refs = load_all_references()
                for msg in messages:
                    chat_id = msg.get("chat_id")
                    content = msg.get("content", "")
                    if not content and not msg.get("attachments"):
                        continue

                    # Find the conversation reference
                    ref_dict = None
                    if chat_id and chat_id in refs:
                        ref_dict = refs[chat_id]
                    elif refs:
                        ref_dict = next(iter(refs.values()))

                    if ref_dict:
                        try:
                            convo_ref = _dict_to_convo_ref(ref_dict)

                            attachments = msg.get("attachments")

                            async def _send_callback(
                                turn_context: TurnContext,
                                text: str = content,
                                atts: list | None = attachments,
                            ) -> None:
                                if atts:
                                    from botbuilder.schema import (
                                        Activity as OutActivity,
                                    )
                                    from botbuilder.schema import Attachment

                                    activity = OutActivity(
                                        type="message",
                                        text=text or "",
                                        attachment_layout="list",
                                        attachments=[
                                            Attachment(
                                                content_type=a.get(
                                                    "contentType",
                                                    "application/vnd.microsoft.card.adaptive",
                                                ),
                                                content=a.get("content"),
                                            )
                                            for a in atts
                                        ],
                                    )
                                    await turn_context.send_activity(activity)
                                else:
                                    await turn_context.send_activity(text)

                            await adapter.continue_conversation(
                                convo_ref,
                                _send_callback,
                                bot_id=ref_dict.get("bot_id", ""),
                            )
                            logger.info("Sent proactive message: %s", content[:50])
                        except Exception as exc:
                            logger.error("Failed to send proactive message: %s", exc)
                    else:
                        logger.warning(
                            "No conversation reference — cannot send: %s",
                            content[:50],
                        )
        except Exception as exc:
            logger.error("Outbound pump error: %s", exc)

        await asyncio.sleep(OUTBOUND_POLL_INTERVAL)


# ── aiohttp app factory ───────────────────────────────────────────


def create_bot_app(adapter: BotFrameworkAdapter, bot: EntraBotBot) -> web.Application:
    """Create the aiohttp web application for the bot server.

    Routes ``/api/messages`` to the Bot Framework adapter which
    authenticates the request and dispatches to the bot handler.
    """
    app = web.Application()

    async def handle_messages(request: web.Request) -> web.Response:
        """Receive activities from Azure Bot Service via the adapter."""
        if request.content_type != "application/json":
            return web.Response(status=415, text="Unsupported media type")

        body = await request.text()
        auth_header = request.headers.get("Authorization", "")

        activity = Activity().deserialize(json.loads(body))

        try:
            response = await adapter.process_activity(activity, auth_header, bot.on_turn)
            if response:
                return web.Response(status=response.status, body=response.body)
            return web.Response(status=200)
        except PermissionError:
            logger.error("Auth failed for activity (check bot app ID / secret)")
            return web.Response(status=401, text="Unauthorized")
        except Exception as exc:
            logger.error("Failed to process activity: %s", exc)
            return web.Response(status=500, text="Internal server error")

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_post("/api/messages", handle_messages)
    app.router.add_get("/health", health)
    return app


async def run_bot_server(port: int = 3978) -> None:
    """Start the bot server on the given port.

    Creates the Bot Framework adapter with credentials from environment.
    Prefers certificate auth (ADR-003) — loads private key from OS keystore.
    Falls back to client secret if cert is not available.
    """
    bot_app_id = os.environ.get("ENTRABOT_BOT_APP_ID", "")
    bot_cert_thumbprint = os.environ.get("ENTRABOT_BOT_CERT_THUMBPRINT", "")
    bot_app_password = os.environ.get("ENTRABOT_BOT_APP_PASSWORD", "")
    tenant_id = os.environ.get("ENTRABOT_TENANT_ID", "")

    adapter = _create_adapter(bot_app_id, bot_cert_thumbprint, bot_app_password, tenant_id)

    async def on_error(context: TurnContext, error: Exception) -> None:
        logger.error("Bot adapter error: %s", error)

    adapter.on_turn_error = on_error

    bot = EntraBotBot()
    app = create_bot_app(adapter, bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()
    logger.info("Bot server listening on http://localhost:%d/api/messages", port)
    logger.info("Bot app ID: %s", bot_app_id or "(none — local dev mode)")

    # Start outbound pump (proactive messaging)
    asyncio.create_task(_outbound_pump(adapter, bot))

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


def _create_adapter(
    app_id: str,
    cert_thumbprint: str,
    app_password: str,
    tenant_id: str,
) -> BotFrameworkAdapter:
    """Create the BotFrameworkAdapter with the best available credentials.

    Priority (per ADR-003 — no secrets on disk):
      1. Certificate from OS keystore (keychain/TPM)
      2. Client secret (fallback for dev)
      3. No credentials (local-only mode)
    """
    if cert_thumbprint and app_id:
        try:
            import keyring

            private_key_pem = keyring.get_password("entrabot-bot", "private-key")
            cert_pem = keyring.get_password("entrabot-bot", "certificate")
            if private_key_pem and cert_pem:
                import hashlib

                from botframework.connector.auth import (
                    CertificateAppCredentials,
                )
                from cryptography import x509
                from cryptography.hazmat.primitives.serialization import Encoding

                # MSAL expects SHA-1 hex thumbprint, but setup_bot.sh
                # stores SHA-256 base64url. Compute SHA-1 from the cert.
                cert_obj = x509.load_pem_x509_certificate(cert_pem.encode())
                der_bytes = cert_obj.public_bytes(Encoding.DER)
                sha1_thumbprint = hashlib.sha1(der_bytes).hexdigest().upper()

                credentials = CertificateAppCredentials(
                    app_id=app_id,
                    certificate_thumbprint=sha1_thumbprint,
                    certificate_private_key=private_key_pem,
                    channel_auth_tenant=tenant_id or None,
                )

                settings = BotFrameworkAdapterSettings(
                    app_id=app_id,
                    app_password="",
                )
                adapter = BotFrameworkAdapter(settings)
                adapter._credentials = credentials
                logger.info(
                    "Using certificate auth (SHA-1: %s...)",
                    sha1_thumbprint[:16],
                )
                return adapter
            else:
                logger.warning(
                    "Cert thumbprint set but key/cert not in keystore — "
                    "falling back to client secret"
                )
        except ImportError:
            logger.warning("keyring not installed — falling back to client secret")

    if app_password and app_id:
        logger.info("Using client secret auth")
        settings = BotFrameworkAdapterSettings(
            app_id=app_id,
            app_password=app_password,
        )
        return BotFrameworkAdapter(settings)

    logger.warning("No bot credentials — running in local-only mode")
    settings = BotFrameworkAdapterSettings(app_id="", app_password="")
    return BotFrameworkAdapter(settings)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("ENTRABOT_LOG_LEVEL", "INFO"))
    port = int(os.environ.get("ENTRABOT_BOT_TUNNEL_PORT", "3978"))
    asyncio.run(run_bot_server(port))
