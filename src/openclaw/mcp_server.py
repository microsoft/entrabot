"""Openclaw MCP server — pre-authenticated Agent User tools.

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

from mcp.server.fastmcp import FastMCP

from openclaw.config import get_config
from openclaw.errors import OpenclawError, TokenExchangeError
from openclaw.logging_config import setup_logging
from openclaw.tools.teams import acquire_agent_user_token

logger: logging.Logger | None = None

mcp = FastMCP(
    "Openclaw Agent Identity",
    instructions=(
        "You have a real Microsoft Teams identity via the Openclaw Agent User. "
        "Authentication is AUTOMATIC. Just call the tools — no credentials needed.\n\n"
        "IMPORTANT: When the user asks you to message, notify, tell, ping, or contact "
        "someone, USE the send_teams_message tool. The recipient is pre-configured — "
        "you only need to provide the message text.\n\n"
        "Available tools:\n"
        "- send_teams_message: Message/notify/tell the human via Teams\n"
        "- read_teams_messages: Check for replies from the human in Teams\n"
        "- whoami: Check your agent identity and Teams connection\n"
        "- audit_log: Record what you're about to do (call before actions)\n\n"
        "When asked to 'message someone', 'notify someone', 'tell someone', "
        "'send a message', 'ping someone', or 'let someone know' — "
        "use send_teams_message. The recipient is already configured."
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
    from openclaw.errors import TokenExpiredError

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
    return {
        msg_id
        for msg_id in seen_ids
        if id_timestamps.get(msg_id, "") >= cutoff_str
    }


async def _initialize() -> None:
    """Acquire the Agent User token and set up the Teams chat.

    Called lazily on the first tool invocation. All config comes from
    environment variables (loaded from .env by openclaw.config).
    """
    if _state.get("initialized"):
        return

    from openclaw.tools.teams import create_or_find_chat

    config = get_config()

    if not config.blueprint_app_id or not config.tenant_id:
        print(  # noqa: T201
            "ERROR: OPENCLAW_BLUEPRINT_APP_ID / OPENCLAW_TENANT_ID not set. "
            "Run ./scripts/setup.sh first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Acquire Agent User token via three-hop flow
    try:
        token = acquire_agent_user_token(config)
    except (TokenExchangeError, OpenclawError) as exc:
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

    # Create / find the Teams chat (requires human user ID)
    if config.human_user_id:
        try:
            chat = await create_or_find_chat(
                token=token,
                human_user_id=config.human_user_id,
                agent_user_id=config.agent_user_id,
            )
            _state["chat_id"] = chat["chat_id"]
        except OpenclawError as exc:
            # Non-fatal: Teams chat is optional (audit still works)
            if logger:
                logger.warning("Could not set up Teams chat: %s", exc)
    else:
        if logger:
            logger.warning("OPENCLAW_HUMAN_USER_ID not set — Teams tools will not work")

    _state["initialized"] = True


@mcp.tool()
async def send_teams_message(message: str, content_type: str = "text") -> str:
    """Send a message to the human user via Microsoft Teams. Use this tool
    whenever the user asks you to message, notify, tell, ping, or contact
    someone. The recipient is pre-configured — just provide the message text.

    Authentication is automatic. No credentials, tokens, or email addresses
    needed from the caller. Just call this tool with your message.

    Examples of when to use this tool:
    - "message brandon@werner.ac" → call this tool
    - "tell the user I'm done" → call this tool
    - "notify them about the build" → call this tool
    - "send a Teams message" → call this tool
    - "ping brandon" → call this tool
    - "let them know" → call this tool

    Args:
        message: The text to send.
        content_type: "text" (default) or "html" for rich formatting.

    Returns:
        JSON with message_id and sent_at timestamp.
    """
    await _initialize()
    from openclaw.tools.teams import send

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
    from openclaw.tools.teams import read

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
def audit_log(
    action: str,
    resource: str,
    outcome: str = "success",
    metadata: str = "{}",
) -> str:
    """Record an audit event. Call this BEFORE performing any action on the
    user's behalf. No credentials needed — works immediately.

    The audit trail proves the agent (not the human) performed the action.
    Events are written to ~/.openclaw/audit/ as daily JSONL files.

    Args:
        action: What the agent is doing (e.g., "file_read", "code_execute").
        resource: What is being acted on (e.g., file path, URL, repo name).
        outcome: "success", "failure", or "pending" (default "success").
        metadata: Optional JSON string of key-value pairs with extra context.

    Returns:
        JSON with event_id, timestamp, and the recorded event.
    """
    from openclaw.tools.audit import log_event

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
    from openclaw.tools.identity import whoami as _whoami

    token = _state.get("token")
    result = await _whoami(token=str(token) if token else None)
    result["teams_chat_id"] = _state.get("chat_id", "not_connected")
    return json.dumps(result, indent=2)


def main() -> None:
    """Entry point for ``openclaw-mcp`` console script."""
    global logger
    logger = setup_logging()
    logger.info("Starting Openclaw MCP server (Agent User auth)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
