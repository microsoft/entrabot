"""Agent-facing Teams tools (replace the MQTT Session/ChannelTools.cs ``channels_*`` set).

These are the agent's *voice* on Teams, so they're ``skip_permission=True`` — the per-caller
permission policy governs the dangerous tools (shell/write/read/url/mcp), not the reply path.
"""

from __future__ import annotations

from typing import Any

import copilot
from pydantic import BaseModel, Field

from .bridge import TeamsBridge, TurnContext

# The agent's voice on Teams — the reply path. The agent can't respond to ANY caller without
# these, so they're locked ON for sponsors and guests alike (never gated by the per-caller
# policy). build_tool_gate always-allows them; the /permissions matrix shows them locked.
TEAMS_TOOL_NAMES = ("entrabot_send", "entrabot_read", "entrabot_list_chats")


class SendArgs(BaseModel):
    message: str = Field(description="The message to send (HTML by default).")
    chat_id: str | None = Field(default=None, description="Target chat; defaults to the active chat.")
    content_type: str = Field(default="html", description='"html" (default) or "text".')


class ReadArgs(BaseModel):
    chat_id: str | None = Field(default=None, description="Chat to read; defaults to the active chat.")
    count: int = Field(default=5, description="How many recent messages to return.")


def _arg(args: Any, key: str, default: Any = None) -> Any:
    if isinstance(args, dict):
        return args.get(key, default)
    return getattr(args, key, default)


def _format_watched(chats: list[str]) -> str:
    if not chats:
        return "(no watched chats)"
    return "watched chats:\n" + "\n".join(f"- {chat}" for chat in chats)


def build_teams_tools(bridge: TeamsBridge, ctx: TurnContext) -> list[Any]:
    async def _send(_ctx: Any, inv: copilot.ToolInvocation) -> str:
        arguments = inv.arguments
        chat = _arg(arguments, "chat_id") or ctx.chat
        if not chat:
            return "error: no chat_id given and no active chat to reply to."
        message = _arg(arguments, "message", "")
        if not message:
            return "error: message is empty."
        result = await bridge.send(chat, message, content_type=_arg(arguments, "content_type", "html"))
        return f"sent to {chat} (message id {result.get('id', '?')})"

    async def _read(_ctx: Any, inv: copilot.ToolInvocation) -> str:
        arguments = inv.arguments
        chat = _arg(arguments, "chat_id") or ctx.chat
        if not chat:
            return "error: no chat_id given and no active chat."
        messages = await bridge.read(chat, count=int(_arg(arguments, "count", 5)))
        lines = [f"- {m.get('from', '?')}: {m.get('content', '')}" for m in messages]
        return "\n".join(lines) if lines else "(no messages)"

    async def _list(_ctx: Any, _inv: copilot.ToolInvocation) -> str:
        return _format_watched(bridge.watched_chats())

    return [
        copilot.define_tool(
            name="entrabot_send",
            description="Send a message to a Microsoft Teams chat (your reply to the caller).",
            handler=_send,
            params_type=SendArgs,
            skip_permission=True,
        ),
        copilot.define_tool(
            name="entrabot_read",
            description="Read recent messages from a Teams chat.",
            handler=_read,
            params_type=ReadArgs,
            skip_permission=True,
        ),
        copilot.define_tool(
            name="entrabot_list_chats",
            description="List the Teams chats this ENTRABOT is watching.",
            handler=_list,
            skip_permission=True,
        ),
    ]
