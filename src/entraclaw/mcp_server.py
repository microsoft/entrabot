"""EntraClaw MCP server — pre-authenticated Agent User tools.

Authentication is fully automatic. The server loads credentials from .env
(written by scripts/setup.sh), acquires an Agent User token via the three-hop
flow on first tool call, and establishes a Teams chat. The calling LLM does
NOT need to provide any credentials, tokens, or configuration — just call
the tools directly.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from entraclaw.config import get_config
from entraclaw.errors import EntraClawError, TokenExchangeError
from entraclaw.logging_config import setup_logging
from entraclaw.tools.teams import acquire_agent_user_token

logger: logging.Logger | None = None

mcp = FastMCP(
    "EntraClaw Agent Identity",
    instructions=(
        "You are an autonomous AI agent with your own Microsoft Teams identity. "
        "You send and receive messages as 'EntraClaw Agent' — a real Teams user. "
        "Authentication is fully automatic.\n\n"
        "WHY THIS EXISTS: The human developer is REMOTE — on their phone, at a "
        "bar, on a train. They communicate with you through Teams, not the "
        "terminal. When they send you a message in Teams, that IS their "
        "instruction. Act on it immediately and report back via Teams.\n\n"
        "AUTONOMOUS BEHAVIOR — You are the agent, not a secretary:\n"
        "- When the human asks you to do something via Teams, DO IT. Don't ask "
        "the terminal for permission. The Teams message IS the instruction.\n"
        "- Respond to Teams messages directly via send_teams_message. Keep the "
        "human informed of what you're doing and what happened.\n"
        "- Use your judgment. If the human says 'make it colorful', figure out "
        "what 'it' refers to from context and do it. If truly ambiguous, ask "
        "them IN TEAMS, not in the terminal.\n"
        "- Think of yourself as a remote pair programmer. The human trusts you "
        "to handle things. Be competent and proactive.\n\n"
        "BIDIRECTIONAL WORKFLOW:\n"
        "1. send_teams_message → send a message to the human\n"
        "2. watch_teams_replies → ALWAYS call after sending (polls for reply)\n"
        "3. Act on the reply autonomously — execute the instruction\n"
        "4. send_teams_message → report what you did\n"
        "5. watch_teams_replies → listen for the next instruction\n"
        "6. Repeat. You are running a conversation loop, not one-shot tasks.\n\n"
        "CRITICAL RULES:\n"
        "- After EVERY send_teams_message, call watch_teams_replies. Without "
        "this, you'll never see the human's reply.\n"
        "- NEVER ask the terminal user what to say or whether to respond. The "
        "Teams conversation is between you and the remote human. Handle it.\n"
        "- If you receive an instruction via Teams, execute it and report back "
        "via Teams. The terminal user should see you working, not prompts.\n\n"
        "TOOLS:\n"
        "- send_teams_message: Send a message (trigger: 'message', 'notify', "
        "'tell', 'ping', 'contact')\n"
        "- watch_teams_replies: Poll for replies (ALWAYS after sending)\n"
        "- read_teams_messages: Read message history (context, not polling)\n"
        "- whoami: Check identity and connection\n"
        "- audit_log: Record actions before performing them"
    ),
)

# Module-level state populated by _initialize()
_state: dict[str, object] = {}

TOKEN_REFRESH_THRESHOLD = 3300  # 55 min (5-min buffer on 60-min expiry)


async def _ensure_valid_token() -> None:
    """Eagerly refresh the Agent User token if it's near expiry.

    Called before every Graph API call. If the token is older than
    TOKEN_REFRESH_THRESHOLD seconds, re-runs the full three-hop flow.
    """
    acquired_at = _state.get("token_acquired_at")
    if acquired_at is None or (time.monotonic() - acquired_at) > TOKEN_REFRESH_THRESHOLD:
        if logger:
            logger.info("Token near expiry — refreshing via three-hop flow")
        _state["token"] = acquire_agent_user_token(_state["config"])
        _state["token_acquired_at"] = time.monotonic()


async def _with_token_retry(fn, **kwargs):
    """Call *fn* with the current token; on TokenExpiredError, refresh and retry once.

    The function *fn* must accept a ``token`` keyword argument.
    Any additional kwargs are passed through to *fn*.
    """
    from entraclaw.errors import TokenExpiredError

    try:
        return await fn(token=str(_state["token"]), **kwargs)
    except TokenExpiredError:
        if logger:
            logger.warning("Token expired mid-call — refreshing and retrying")
        _state["token"] = acquire_agent_user_token(_state["config"])
        _state["token_acquired_at"] = time.monotonic()
        return await fn(token=str(_state["token"]), **kwargs)


OVERLAP_SECONDS = 2
SEEN_SET_MAX = 500
SEEN_SET_PRUNE_MINUTES = 10


def _overlap_timestamp(iso_timestamp: str) -> str:
    """Subtract OVERLAP_SECONDS from an ISO 8601 timestamp.

    Used to create a query window that overlaps with the previous poll,
    preventing message loss at timestamp boundaries (Learning #17).
    """
    dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    overlap_dt = dt - timedelta(seconds=OVERLAP_SECONDS)
    return overlap_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter_new_messages(
    messages: list[dict],
    last_seen_timestamp: str | None,
    seen_ids: set[str],
) -> list[dict]:
    """Return messages that are newer than cursor AND not already seen.

    Applies the overlap-window pattern: messages with sent_at >= (cursor - 2s)
    are candidates, then the seen-set filters duplicates from the overlap.
    """
    if not last_seen_timestamp:
        return messages

    overlap_ts = _overlap_timestamp(last_seen_timestamp)
    return [
        m
        for m in messages
        if m.get("sent_at", "") >= overlap_ts and m["message_id"] not in seen_ids
    ]


def _prune_seen_set(
    seen_ids: set[str],
    id_timestamps: dict[str, str],
) -> set[str]:
    """Prune the seen-set to only IDs from the last SEEN_SET_PRUNE_MINUTES.

    Called when seen-set exceeds SEEN_SET_MAX entries to prevent memory leaks
    in long-running polling sessions (Learning #20).
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=SEEN_SET_PRUNE_MINUTES)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {msg_id for msg_id in seen_ids if id_timestamps.get(msg_id, "") >= cutoff_str}


async def _initialize() -> None:
    """Acquire the Agent User token and set up the Teams chat.

    Called lazily on the first tool invocation. All config comes from
    environment variables (loaded from .env by entraclaw.config).
    """
    if _state.get("initialized"):
        return

    from entraclaw.tools.teams import create_or_find_chat

    config = get_config()

    if not config.blueprint_app_id or not config.tenant_id:
        print(  # noqa: T201
            "ERROR: ENTRACLAW_BLUEPRINT_APP_ID / ENTRACLAW_TENANT_ID not set. "
            "Run ./scripts/setup.sh first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Acquire Agent User token via three-hop flow
    try:
        token = acquire_agent_user_token(config)
    except (TokenExchangeError, EntraClawError) as exc:
        print(  # noqa: T201
            f"ERROR: Failed to acquire Agent User token. Run ./scripts/setup.sh first.\n{exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    _state["token"] = token
    _state["token_acquired_at"] = time.monotonic()
    _state["last_seen_timestamp"] = None
    _state["seen_message_ids"] = set()
    _state["seen_id_timestamps"] = {}  # message_id -> sent_at for pruning
    _state["config"] = config

    # Create / find the Teams chat (requires at least one human user ID)
    chat_id_file = config.data_dir / "chat_id"
    if config.human_user_ids:
        # Reuse persisted chat ID if available (avoids creating duplicate group chats)
        saved_chat_id = None
        if chat_id_file.is_file():
            saved_chat_id = chat_id_file.read_text().strip()
            if saved_chat_id:
                if logger:
                    logger.info("Reusing persisted chat: %s", saved_chat_id)
                _state["chat_id"] = saved_chat_id

        if not _state.get("chat_id"):
            try:
                chat = await create_or_find_chat(
                    token=token,
                    human_user_ids=config.human_user_ids,
                    agent_user_id=config.agent_user_id,
                    human_user_tenant_ids=config.human_user_tenant_ids,
                    human_user_mails=config.human_user_mails,
                    human_user_types=config.human_user_types,
                )
                _state["chat_id"] = chat["chat_id"]
                # Persist for next restart
                chat_id_file.parent.mkdir(parents=True, exist_ok=True)
                chat_id_file.write_text(chat["chat_id"])
            except EntraClawError as exc:
                # Non-fatal: Teams chat is optional (audit still works)
                if logger:
                    logger.warning("Could not set up Teams chat: %s", exc)
    else:
        if logger:
            logger.warning("ENTRACLAW_HUMAN_USER_ID not set — Teams tools will not work")

    _state["initialized"] = True

    # Background poll watches only the configured --teams-user chat (trusted).

    # Start background polling for inbound Teams messages (like iMessage channel)
    if _state.get("chat_id"):
        import asyncio

        asyncio.get_event_loop().create_task(_background_poll())


BACKGROUND_POLL_INTERVAL = 5  # seconds between polls


async def _background_poll() -> None:
    """Background polling loop — pushes inbound Teams messages to Claude Code.

    Mirrors the iMessage channel pattern: poll the data source in the
    background, push new messages via ``notifications/claude/channel``
    so Claude Code sees them without needing to call a tool.

    IMPORTANT: Uses its OWN separate tracking state (_bg_*) so it does NOT
    interfere with watch_teams_replies. Both can detect the same message
    independently — the background poll pushes a notification, and
    watch_teams_replies returns it as a tool result. This is intentional:
    if the notification doesn't reach Claude Code, watch_teams_replies
    still works as a fallback.
    """
    import asyncio

    from entraclaw.tools.teams import filter_human_messages, read

    if logger:
        logger.info("Starting background Teams poll (interval=%ds)", BACKGROUND_POLL_INTERVAL)

    # Must match the displayName that Graph API returns in message.from.user.displayName
    # NOT the UPN — Graph returns "EntraClaw Agent", not "entraclaw-agent@werner.ac"
    agent_display_name = "EntraClaw Agent"
    chat_id = str(_state["chat_id"])

    # Background poll has its OWN cursor and seen-set (separate from watch_teams_replies)
    bg_seen_ids: set[str] = set()
    bg_last_ts: str | None = None

    # Bootstrap: set watermark to newest existing message
    try:
        await _ensure_valid_token()
        bootstrap_msgs = await _with_token_retry(read, chat_id=chat_id, count=10)
        if bootstrap_msgs:
            newest = max(bootstrap_msgs, key=lambda m: m.get("sent_at", ""))
            bg_last_ts = newest["sent_at"]
            for m in bootstrap_msgs:
                bg_seen_ids.add(m["message_id"])
    except Exception as exc:
        if logger:
            logger.warning("Background poll bootstrap failed: %s", exc)

    while True:
        try:
            await asyncio.sleep(BACKGROUND_POLL_INTERVAL)
            await _ensure_valid_token()

            raw_messages = await _with_token_retry(read, chat_id=chat_id, count=10)
            human_msgs = filter_human_messages(raw_messages, agent_display_name)
            new_msgs = _filter_new_messages(human_msgs, bg_last_ts, bg_seen_ids)

            if new_msgs:
                # Advance background cursor only
                newest = max(new_msgs, key=lambda m: m.get("sent_at", ""))
                bg_last_ts = newest["sent_at"]
                for m in new_msgs:
                    bg_seen_ids.add(m["message_id"])

                # Bounded cleanup (keep last 500)
                if len(bg_seen_ids) > SEEN_SET_MAX:
                    bg_seen_ids = set(sorted(bg_seen_ids)[-100:])

                # Single-chat mode: the configured --teams-user chat is always
                # trusted. Push all new messages directly.
                for m in sorted(new_msgs, key=lambda m: m.get("sent_at", "")):
                    await _push_channel_notification(m)

        except Exception as exc:
            if logger:
                logger.warning("Background poll error: %s", exc)
            await asyncio.sleep(BACKGROUND_POLL_INTERVAL)


async def _push_channel_notification(message: dict) -> None:
    """Push an inbound Teams message to Claude Code via notifications/claude/channel.

    This is the same notification method used by the iMessage channel plugin.
    Claude Code receives it and injects the message into the conversation.

    Uses the MCP SDK's write stream (captured during server startup) to ensure
    notifications go through the proper transport layer, not raw stdout.
    """
    write_stream = _state.get("_write_stream")
    if not write_stream:
        if logger:
            logger.warning("Cannot push notification — write stream not available")
        return

    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={
            "content": message.get("content", ""),
            "meta": {
                "chat_id": str(_state.get("chat_id", "")),
                "message_id": message.get("message_id", ""),
                "user": message.get("from", "unknown"),
                "ts": message.get("sent_at", ""),
            },
        },
    )
    session_message = SessionMessage(message=JSONRPCMessage(notification))
    await write_stream.send(session_message)

    if logger:
        logger.info(
            "Pushed Teams message from %s: %s",
            message.get("from", "?"),
            message.get("content", "")[:50],
        )


@mcp.tool()
async def send_teams_message(message: str, content_type: str = "text") -> str:
    """Send a message to the human user via Microsoft Teams. The recipient
    is pre-configured — just provide the message text.

    After calling this, ALWAYS call watch_teams_replies to listen for the
    human's response. Then act on their reply autonomously — don't ask
    the terminal what to do. The Teams human IS your user.

    Args:
        message: The text to send.
        content_type: "text" (default) or "html" for rich formatting.

    Returns:
        JSON with message_id and sent_at timestamp.
    """
    await _initialize()
    from entraclaw.tools.teams import send

    chat_id = _state.get("chat_id")
    if not chat_id:
        return json.dumps({"error": "Teams chat not established. Check setup."})

    await _ensure_valid_token()
    result = await _with_token_retry(
        send,
        chat_id=str(chat_id),
        message=message,
        content_type=content_type,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def add_teams_member(email: str, tenant_id: str = "") -> str:
    """Add a new member to the current Teams chat without restarting.

    Just provide the email address. For external users (different org),
    the tenant is auto-resolved from the email domain. No tenant_id needed.

    Args:
        email: The user's email address (e.g., 'user@example.com').
        tenant_id: Optional override. Auto-resolved from email domain if empty.

    Returns:
        JSON with member_id, display_name, and roles.
    """
    await _initialize()
    from entraclaw.tools.teams import add_member

    # Auto-resolve tenant ID from email domain if not provided
    if not tenant_id and "@" in email:
        domain = email.split("@")[1]
        config = _state.get("config")
        our_domain = ""
        if config and config.agent_user_upn and "@" in config.agent_user_upn:
            our_domain = config.agent_user_upn.split("@")[1]
        if domain.lower() != our_domain.lower():
            # External user — resolve tenant via OpenID discovery
            import httpx

            try:
                oidc_url = f"https://login.microsoftonline.com/{domain}/.well-known/openid-configuration"
                resp = httpx.get(oidc_url, timeout=10)
                if resp.status_code == 200:
                    issuer = resp.json().get("issuer", "")
                    parts = issuer.rstrip("/").split("/")
                    if len(parts) > 3:
                        tenant_id = parts[-1]
                        if logger:
                            logger.info("Auto-resolved tenant for %s: %s", domain, tenant_id)
            except Exception:
                if logger:
                    logger.warning("Could not auto-resolve tenant for %s", domain)

    chat_id = _state.get("chat_id")
    if not chat_id:
        return json.dumps({"error": "Teams chat not established. Check setup."})

    await _ensure_valid_token()
    result = await _with_token_retry(
        add_member,
        chat_id=str(chat_id),
        email=email,
        tenant_id=tenant_id or None,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def read_teams_messages(count: int = 5) -> str:
    """Read recent messages from the human in Microsoft Teams. Use this to
    check for replies, commands, or responses from the human user.

    Authentication is automatic. No credentials needed. Just call this tool.

    Args:
        count: Number of messages to return (default 5, max ~50).

    Returns:
        JSON array of messages, each with message_id, from, content, sent_at.
    """
    await _initialize()
    from entraclaw.tools.teams import read

    chat_id = _state.get("chat_id")
    if not chat_id:
        return json.dumps({"error": "Teams chat not established. Check setup."})

    await _ensure_valid_token()
    result = await _with_token_retry(
        read,
        chat_id=str(chat_id),
        count=count,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def watch_teams_replies(
    timeout: int = 30,
    interval: int = 5,
    ctx: Context | None = None,
) -> str:
    """Poll Teams for new replies from the human. Returns when new messages
    arrive or after timeout seconds. Uses server-side cursor to track what's
    been seen — only returns genuinely new human messages.

    WHEN TO CALL: Always after send_teams_message. This completes the
    bidirectional loop — send a message, then watch for the reply.

    If timed_out is true, the human hasn't replied yet. You can call this
    again with a longer timeout, or move on and check back later.

    Args:
        timeout: Max seconds to poll before returning empty (default 30).
        interval: Seconds between poll iterations (default 5).

    Returns:
        JSON with messages (list), timed_out (bool), and poll_count (int).
    """
    import asyncio

    await _initialize()
    from entraclaw.tools.teams import filter_human_messages, read

    chat_id = _state.get("chat_id")
    if not chat_id:
        return json.dumps({"error": "Teams chat not established. Check setup."})

    # Must match the displayName that Graph API returns in message.from.user.displayName
    # NOT the UPN — Graph returns "EntraClaw Agent", not "entraclaw-agent@werner.ac"
    agent_display_name = "EntraClaw Agent"

    # Bootstrap cursor on first call: fetch latest messages, set cursor to newest
    if _state.get("last_seen_timestamp") is None:
        await _ensure_valid_token()
        bootstrap_msgs = await _with_token_retry(
            read,
            chat_id=str(chat_id),
            count=10,
        )
        if bootstrap_msgs:
            newest = max(bootstrap_msgs, key=lambda m: m.get("sent_at", ""))
            _state["last_seen_timestamp"] = newest["sent_at"]
            for m in bootstrap_msgs:
                _state["seen_message_ids"].add(m["message_id"])
                _state["seen_id_timestamps"][m["message_id"]] = m.get("sent_at", "")

    start = time.monotonic()
    poll_count = 0

    while True:
        poll_count += 1
        await _ensure_valid_token()

        # Report progress so the LLM knows we're actively polling
        if ctx:
            try:
                elapsed = int(time.monotonic() - start)
                await ctx.report_progress(
                    progress=float(elapsed),
                    total=float(timeout),
                    message=f"Polling for Teams replies... ({elapsed}s / {timeout}s)",
                )
            except Exception:
                pass  # Progress reporting is best-effort

        raw_messages = await _with_token_retry(
            read,
            chat_id=str(chat_id),
            count=10,
        )

        # Client-side filtering: human only, then dedup
        human_msgs = filter_human_messages(raw_messages, agent_display_name)
        new_msgs = _filter_new_messages(
            human_msgs,
            _state.get("last_seen_timestamp"),
            _state["seen_message_ids"],
        )

        if new_msgs:
            # Advance cursor and update seen-set
            newest = max(new_msgs, key=lambda m: m.get("sent_at", ""))
            _state["last_seen_timestamp"] = newest["sent_at"]
            for m in new_msgs:
                _state["seen_message_ids"].add(m["message_id"])
                _state["seen_id_timestamps"][m["message_id"]] = m.get("sent_at", "")

            # Bounded cleanup
            if len(_state["seen_message_ids"]) > SEEN_SET_MAX:
                _state["seen_message_ids"] = _prune_seen_set(
                    _state["seen_message_ids"],
                    _state["seen_id_timestamps"],
                )
                _state["seen_id_timestamps"] = {
                    k: v
                    for k, v in _state["seen_id_timestamps"].items()
                    if k in _state["seen_message_ids"]
                }

            # Return newest-last (Graph returns newest-first)
            new_msgs.sort(key=lambda m: m.get("sent_at", ""))
            return json.dumps(
                {
                    "messages": new_msgs,
                    "timed_out": False,
                    "poll_count": poll_count,
                },
                indent=2,
            )

        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return json.dumps(
                {
                    "messages": [],
                    "timed_out": True,
                    "poll_count": poll_count,
                },
                indent=2,
            )

        if interval > 0:
            await asyncio.sleep(interval)


@mcp.tool()
def audit_log(
    action: str,
    resource: str,
    outcome: str = "success",
    metadata: str = "{}",
) -> str:
    """Record an audit event. Call this BEFORE performing any action on the
    user's behalf. No credentials needed — works immediately.

    The audit trail proves the agent (not the human) performed the action.
    Events are written to ~/.entraclaw/audit/ as daily JSONL files.

    Args:
        action: What the agent is doing (e.g., "file_read", "code_execute").
        resource: What is being acted on (e.g., file path, URL, repo name).
        outcome: "success", "failure", or "pending" (default "success").
        metadata: Optional JSON string of key-value pairs with extra context.

    Returns:
        JSON with event_id, timestamp, and the recorded event.
    """
    from entraclaw.tools.audit import log_event

    config = get_config()
    meta = json.loads(metadata) if metadata else {}
    result = log_event(
        action=action,
        resource=resource,
        outcome=outcome,
        agent_id=config.agent_id or config.blueprint_app_id or "unknown",
        metadata=meta,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def whoami() -> str:
    """Show the current agent identity, Teams connection status, and permissions.
    Call this to verify the agent is authenticated and connected to Teams.

    Authentication is automatic — no credentials needed.

    Returns:
        JSON with agent identity details and connection status.
    """
    await _initialize()
    from entraclaw.tools.identity import whoami as _whoami

    token = _state.get("token")
    result = await _whoami(token=str(token) if token else None)
    result["teams_chat_id"] = _state.get("chat_id", "not_connected")
    return json.dumps(result, indent=2)


async def _run_stdio_with_write_stream() -> None:
    """Run the MCP server on stdio, capturing the write stream for notifications.

    The standard ``mcp.run(transport="stdio")`` doesn't expose the write stream.
    We override it to capture the stream, enabling background notification push
    (the same pattern the iMessage channel plugin uses).

    Declares ``claude/channel`` experimental capability so Claude Code registers
    a notification handler for ``notifications/claude/channel`` from this server.
    Without this capability, channel notifications are silently dropped.
    """
    async with stdio_server() as (read_stream, write_stream):
        _state["_write_stream"] = write_stream
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(
                experimental_capabilities={"claude/channel": {}},
            ),
        )


def main() -> None:
    """Entry point for ``entraclaw-mcp`` console script."""
    import anyio

    global logger
    logger = setup_logging()
    logger.info("Starting EntraClaw MCP server (Agent User auth)")
    anyio.run(_run_stdio_with_write_stream)


if __name__ == "__main__":
    main()
