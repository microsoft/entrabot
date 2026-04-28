"""Long-blocking MCP tool: wait until a sponsor DMs the agent in Teams.

This is the primary integration path for Copilot CLI, which does NOT
plumb FastMCP server ``instructions=`` into the LLM system prompt and
does NOT subscribe to the experimental ``notifications/claude/channel``
push (Claude Code does both — see ``docs/runbooks/hard-won-learnings.md``
Learning #54).

The tool sleeps inside the SAME MCP session as the working agent —
no spawned ``copilot -p`` daemon, no PTY supervisor — so the operator
can keep typing, hit Ctrl+C to abort the wait at any time, and the
sponsor's DM becomes the agent's next turn input by appearing in the
tool's return value.

Sponsor gating is mechanical (this module enforces it). The agent
cannot accidentally process a non-sponsor message because non-sponsor
messages never reach the return value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_HEARTBEAT_S = 30.0
DEDUP_MAX = 1000


@dataclass
class WaitForSponsorDmResult:
    """Structured return value for ``wait_for_sponsor_dm``."""

    chat_id: str
    message_id: str
    sender: str
    sender_id: str
    sent_at: str
    content_text: str
    content_html: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timed_out: bool = False
    chat_type: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "sender": self.sender,
                "sender_id": self.sender_id,
                "sent_at": self.sent_at,
                "content_text": self.content_text,
                "content_html": self.content_html,
                "metadata": self.metadata,
                "timed_out": self.timed_out,
                "chat_type": self.chat_type,
            },
            ensure_ascii=False,
        )

    @classmethod
    def timeout(cls, *, timeout_seconds: int) -> WaitForSponsorDmResult:
        """Construct a structured timeout result so callers (the host LLM) can
        see that no sponsor DM arrived within the window and decide whether to
        wait again. Avoids surfacing a bare ``TimeoutError`` as an empty MCP
        error."""
        return cls(
            chat_id="",
            message_id="",
            sender="",
            sender_id="",
            sent_at="",
            content_text="",
            content_html=None,
            metadata={"timeout_seconds": int(timeout_seconds)},
            timed_out=True,
            chat_type="",
        )


def _injection_dedupe_key(message: dict[str, Any]) -> tuple[str, str] | None:
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        chat_id = str(message.get("chat_id") or "").strip()
        return (chat_id, message_id)
    interaction_id = str(message.get("interaction_id") or "").strip()
    if interaction_id:
        return ("interaction", interaction_id)
    return None


def select_sponsor_message(
    messages: list[dict[str, Any]],
    *,
    gate: Any,
    dedup: deque[tuple[str, str]],
    after_iso: str | None = None,
) -> dict[str, Any] | None:
    """Return the oldest sponsor-accepted message we have not seen yet.

    *gate* must implement ``accepts(message)`` (see
    :class:`entraclaw.identity.sponsors.SponsorGate`). Returns the
    message dict, or ``None`` if there is no eligible message.
    """
    seen_keys = set(dedup)
    eligible: list[dict[str, Any]] = []
    rejected_by_gate: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        key = _injection_dedupe_key(msg)
        if key is None or key in seen_keys:
            continue
        if after_iso:
            sent = str(msg.get("sent_at") or msg.get("received_at") or "")
            if sent and sent <= after_iso:
                continue
        if not gate.accepts(msg):
            rejected_by_gate.append(msg)
            continue
        eligible.append(msg)
    if not eligible:
        # Diagnostic: when we saw fresh, non-dedup'd messages but the gate
        # rejected every one, log identity of rejections so the operator
        # can compare against the loaded sponsor set. Common cause: B2B
        # guest sponsors whose home-tenant identity does not match the
        # guest UPN/object-id stored on the agent's sponsors collection.
        if rejected_by_gate:
            for msg in rejected_by_gate[:5]:
                logger.info(
                    "wait_for_sponsor_dm: gate rejected message "
                    "chat=%s sender_id=%s sender=%s from=%s",
                    msg.get("chat_id") or "",
                    msg.get("sender_id") or "",
                    msg.get("sender") or "",
                    msg.get("from") or "",
                )
        return None
    eligible.sort(key=lambda m: str(m.get("sent_at") or m.get("received_at") or ""))
    return eligible[0]


def _utcnow_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _heartbeat_seconds() -> float:
    raw = os.environ.get("WAIT_TOOL_HEARTBEAT_S")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_HEARTBEAT_S


def _poll_seconds() -> float:
    raw = os.environ.get("WAIT_TOOL_POLL_INTERVAL_S")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_POLL_INTERVAL_S


async def wait_loop(
    *,
    list_chat_ids: Callable[[], list[str]],
    read_chat: Callable[[str], Awaitable[list[dict[str, Any]]]],
    gate: Any,
    dedup: deque[tuple[str, str]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    heartbeat: Callable[[], Awaitable[None]] | None = None,
    started_at_iso: str | None = None,
    poll_interval_s: float | None = None,
    heartbeat_interval_s: float | None = None,
) -> dict[str, Any]:
    """Block until a sponsor DM arrives in any watched chat.

    The caller injects ``list_chat_ids`` and ``read_chat`` so this loop
    can be tested without httpx, and so production code reuses the
    background poll's ``read``+token-retry path.
    """
    started_at = started_at_iso or _utcnow_iso()
    interval = poll_interval_s if poll_interval_s is not None else _poll_seconds()
    hb_interval = (
        heartbeat_interval_s
        if heartbeat_interval_s is not None
        else _heartbeat_seconds()
    )
    last_heartbeat = 0.0
    elapsed = 0.0
    while True:
        chat_ids = list(list_chat_ids() or [])
        for chat_id in chat_ids:
            try:
                messages = await read_chat(chat_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "wait_for_sponsor_dm: read failed for chat %s: %s",
                    chat_id,
                    exc,
                )
                continue
            for msg in messages or []:
                if isinstance(msg, dict) and not msg.get("chat_id"):
                    msg["chat_id"] = chat_id
            picked = select_sponsor_message(
                messages or [],
                gate=gate,
                dedup=dedup,
                after_iso=started_at,
            )
            if picked is not None:
                key = _injection_dedupe_key(picked)
                if key is not None:
                    dedup.append(key)
                    while len(dedup) > DEDUP_MAX:
                        dedup.popleft()
                return picked
        await sleep(interval)
        elapsed += interval
        if heartbeat is not None and elapsed - last_heartbeat >= hb_interval:
            try:
                await heartbeat()
            except Exception as exc:
                logger.debug("wait_for_sponsor_dm: heartbeat failed: %s", exc)
            last_heartbeat = elapsed
