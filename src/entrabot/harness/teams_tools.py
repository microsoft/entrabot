"""Agent-facing Teams tools (replace the MQTT Session/ChannelTools.cs ``channels_*`` set).

These are the agent's *voice* on Teams, so they're ``skip_permission=True`` — the per-caller
permission policy governs the dangerous tools (shell/write/read/url/mcp), not the reply path.
"""

from __future__ import annotations

from typing import Any, List

import copilot
from pydantic import BaseModel, Field

from .teams_comms import TeamsBridge, TurnContext


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


def build_teams_tools(bridge: TeamsBridge, ctx: TurnContext) -> List[Any]:
    async def _send(_ctx: Any, inv: copilot.ToolInvocation) -> str:
        a = inv.arguments
        chat = _arg(a, "chat_id") or ctx.chat
        if not chat:
            return "error: no chat_id given and no active chat to reply to."
        message = _arg(a, "message", "")
        if not message:
            return "error: message is empty."
        res = await bridge.send(chat, message, content_type=_arg(a, "content_type", "html"))
        return f"sent to {chat} (message id {res.get('id', '?')})"

    async def _read(_ctx: Any, inv: copilot.ToolInvocation) -> str:
        a = inv.arguments
        chat = _arg(a, "chat_id") or ctx.chat
        if not chat:
            return "error: no chat_id given and no active chat."
        msgs = await bridge.read(chat, count=int(_arg(a, "count", 5)))
        lines = [f"- {m.get('from', '?')}: {m.get('content', '')}" for m in msgs]
        return "\n".join(lines) if lines else "(no messages)"

    async def _list(_ctx: Any, _inv: copilot.ToolInvocation) -> str:
        chats = bridge.watched_chats()
        return "watched chats:\n" + "\n".join(f"- {c}" for c in chats) if chats else "(no watched chats)"

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
