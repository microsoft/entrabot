"""Openclaw MCP server — pre-authenticated, simple tools.

The server loads credentials from ``.env`` (written by ``scripts/setup.sh``),
acquires an agent token via ROPC, and creates the Teams chat on startup.
NO device-code flows.  NO app registration creation.  If authentication
fails, the server prints an error and exits.

Run directly with ``python -m openclaw.mcp_server`` or via the
``openclaw-mcp`` console script.
"""

from __future__ import annotations

import json
import logging
import sys

from mcp.server.fastmcp import FastMCP

from openclaw.config import get_config
from openclaw.errors import MSALError, OpenclawError
from openclaw.logging_config import setup_logging

logger: logging.Logger | None = None

mcp = FastMCP("Openclaw Agent Identity")

# Module-level state populated by _initialize()
_state: dict[str, object] = {}


async def _initialize() -> None:
    """Acquire the agent token and set up the Teams chat.

    Called lazily on the first tool invocation.  All config comes from
    environment variables (loaded from ``.env`` by ``openclaw.config``).
    """
    if _state.get("initialized"):
        return

    from openclaw.tools.teams import acquire_agent_token, create_or_find_chat

    config = get_config()

    if not config.client_id or not config.tenant_id:
        print(  # noqa: T201
            "ERROR: OPENCLAW_CLIENT_ID / OPENCLAW_TENANT_ID not set. Run ./scripts/setup.sh first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Acquire delegated token for the agent user via ROPC
    try:
        token = acquire_agent_token(config)
    except (MSALError, OpenclawError) as exc:
        print(  # noqa: T201
            f"ERROR: Failed to acquire agent token. Run ./scripts/setup.sh first.\n{exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    _state["token"] = token
    _state["config"] = config

    # Create / find the Teams chat (requires both user IDs)
    if config.agent_user_id and config.human_user_id:
        try:
            chat = await create_or_find_chat(
                token=token,
                agent_user_id=config.agent_user_id,
                human_user_id=config.human_user_id,
            )
            _state["chat_id"] = chat["chat_id"]
        except OpenclawError as exc:
            # Non-fatal: Teams chat is optional (audit still works)
            if logger:
                logger.warning("Could not set up Teams chat: %s", exc)
    else:
        if logger:
            logger.warning(
                "OPENCLAW_AGENT_USER_ID or OPENCLAW_HUMAN_USER_ID not set — "
                "Teams tools will not work"
            )

    _state["initialized"] = True


@mcp.tool()
async def openclaw_teams_send(message: str, content_type: str = "text") -> str:
    """Send a message to the human user in Microsoft Teams.

    The message is sent FROM the Openclaw Agent user (not the human user).
    Use this to report status, results, or ask questions.
    """
    await _initialize()
    from openclaw.tools.teams import send

    chat_id = _state.get("chat_id")
    if not chat_id:
        return json.dumps({"error": "Teams chat not established. Check setup."})

    token = _state.get("token")
    if not token:
        return json.dumps({"error": "No agent token. Run ./scripts/setup.sh first."})

    result = await send(
        chat_id=str(chat_id),
        message=message,
        token=str(token),
        content_type=content_type,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def openclaw_teams_read(count: int = 5) -> str:
    """Read recent messages from the human in the Teams chat.

    Use this to check if the human has sent any commands or responses.
    """
    await _initialize()
    from openclaw.tools.teams import read

    chat_id = _state.get("chat_id")
    if not chat_id:
        return json.dumps({"error": "Teams chat not established. Check setup."})

    token = _state.get("token")
    if not token:
        return json.dumps({"error": "No agent token. Run ./scripts/setup.sh first."})

    result = await read(
        chat_id=str(chat_id),
        token=str(token),
        count=count,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def openclaw_audit_log(
    action: str,
    resource: str,
    outcome: str = "success",
    metadata: str = "{}",
) -> str:
    """Record an audit event. Call this BEFORE performing any action on the user's behalf.

    The audit trail proves the agent (not the human) performed the action.
    metadata should be a JSON string of key-value pairs.
    Events are written to ~/.openclaw/audit/ as daily JSONL files.
    """
    from openclaw.tools.audit import log_event

    config = get_config()
    meta = json.loads(metadata) if metadata else {}
    result = log_event(
        action=action,
        resource=resource,
        outcome=outcome,
        agent_id=config.agent_upn or config.client_id or "unknown",
        metadata=meta,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def openclaw_whoami() -> str:
    """Show the current agent identity, permissions, and connection status."""
    await _initialize()
    from openclaw.tools.identity import whoami

    token = _state.get("token")
    result = await whoami(token=str(token) if token else None)
    result["teams_chat_id"] = _state.get("chat_id", "not_connected")
    return json.dumps(result, indent=2)


def main() -> None:
    """Entry point for ``openclaw-mcp`` console script."""
    global logger
    logger = setup_logging()
    logger.info("Starting Openclaw MCP server (pre-authenticated)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
