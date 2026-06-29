"""Body-side bootstrap — single packet of operational state for session-start.

Issue #20: counterpart to persona-sati's ``bootstrap_session``. Returns
an INDEX of the agent's recent operational activity (counts, top chats,
open promises, cursor freshness) so the model has continuity at the top
of a turn without having to call multiple read tools.

Key design constraint: this is an INDEX, not content. Full interaction
summaries do not appear in the payload — :func:`read_interactions`
serves that on demand. Keeping bootstrap small means it can land in
context without dominating it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from entrabot.config import get_config
from entrabot.tools import chat_cursors
from entrabot.tools.interaction_log import _interaction_key
from entrabot.tools.promises import list_promises
from entrabot.tools.read_interactions import _entry_chat_id, _load_day

logger = logging.getLogger("entrabot.tools.body_bootstrap")

_DESCRIPTION_PREVIEW_LEN = 80
_TOP_CHATS_LIMIT = 5


def _today_entries() -> list[dict]:
    """Load today's interaction JSONL via the configured backend."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    # Use _load_day so corrupt-line handling matches read_interactions.
    # (_load_day reads _interaction_key(day) via get_backend().)
    _ = _interaction_key  # keep import path explicit for grep
    return _load_day(today)


def _summarize_today(entries: list[dict]) -> dict:
    by_action: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    inbound = 0
    outbound = 0
    for e in entries:
        direction = e.get("direction")
        if direction == "inbound":
            inbound += 1
        elif direction == "outbound":
            outbound += 1
        action = e.get("action")
        if action:
            by_action[action] = by_action.get(action, 0) + 1
        channel = e.get("channel")
        if channel:
            by_channel[channel] = by_channel.get(channel, 0) + 1
    return {
        "total": len(entries),
        "inbound": inbound,
        "outbound": outbound,
        "by_action": by_action,
        "by_channel": by_channel,
    }


def _top_chats(entries: list[dict]) -> list[dict]:
    """Top chats by interaction count today; ties broken by recency."""
    by_chat: dict[str, dict] = {}
    for e in entries:
        cid = _entry_chat_id(e)
        if not cid:
            continue
        ts = e.get("ts") or ""
        sender = e.get("sender") or ""
        slot = by_chat.setdefault(
            cid,
            {"chat_id": cid, "interaction_count": 0, "last_activity": "", "last_sender": ""},
        )
        slot["interaction_count"] += 1
        if ts > slot["last_activity"]:
            slot["last_activity"] = ts
            slot["last_sender"] = sender
    ordered = sorted(
        by_chat.values(),
        key=lambda s: (s["interaction_count"], s["last_activity"]),
        reverse=True,
    )
    return ordered[:_TOP_CHATS_LIMIT]


def _open_promises_index() -> list[dict]:
    """Return ALL open promises as compact index entries (no top-N cap)."""

    def _drive() -> list:
        return asyncio.run(list_promises(open_only=True))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        promises = _drive()
    else:
        # Running inside an event loop (e.g. async test context). Drive the
        # coroutine in a worker thread so we don't deadlock — and so we
        # don't leak an un-awaited coroutine by letting asyncio.run raise
        # after constructing the call.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as ex:
            promises = ex.submit(_drive).result()
    out: list[dict] = []
    for p in promises:
        desc = p.description or ""
        preview = desc[:_DESCRIPTION_PREVIEW_LEN]
        out.append(
            {
                "id": p.id,
                "chat_id": p.chat_id,
                "description_preview": preview,
                "created_at": p.created_at,
                "due_by": p.due_by,
            }
        )
    return out


def _cursor_freshness() -> dict:
    """Summarize watched-chat cursor health."""
    from entrabot.storage.backend import get_backend

    backend = get_backend()
    keys = [k for k in backend.list(prefix="chat_cursors/") if k.endswith(".json")]
    cursors_present = 0
    cursors_stale = 0
    timestamps: list[str] = []
    for key in keys:
        raw = backend.read_text(key)
        if raw is None:
            continue
        try:
            import json

            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        cursors_present += 1
        last_ts = payload.get("last_ts")
        # Staleness is judged by when the cursor was last written, not by the
        # newest-message watermark — an idle chat's cursor is fresh even when
        # its newest message is old. (See chat_cursors.is_stale.)
        if chat_cursors.is_stale(payload.get("last_written_at")):
            cursors_stale += 1
        if last_ts:
            timestamps.append(last_ts)
    return {
        "watched_chat_count": _count_watched_chats(),
        "cursors_present": cursors_present,
        "cursors_stale": cursors_stale,
        "oldest_cursor_ts": min(timestamps) if timestamps else None,
        "newest_cursor_ts": max(timestamps) if timestamps else None,
    }


def _count_watched_chats() -> int:
    """Read the persisted watched_chats file; missing → 0."""
    cfg = get_config()
    f = cfg.data_dir / "watched_chats"
    if not f.is_file():
        return 0
    return sum(1 for line in f.read_text().splitlines() if line.strip())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def bootstrap_body_state() -> dict:
    """Return a single packet of body-side state for session-start.

    Mirrors persona-sati's ``bootstrap_session`` shape: one call, one
    JSON object the model can scan in a single read. Index only —
    full interaction content stays in :func:`read_interactions`.

    Returns:
        ``today_counts`` — totals, inbound/outbound, by_action, by_channel
            for entries on today's (UTC) interaction log file.
        ``top_chats_today`` — up to 5 chats by interaction count today;
            ties broken by most-recent activity. Each entry: chat_id,
            interaction_count, last_activity, last_sender.
        ``open_promises`` — every open promise (no top-N cap, since
            commitments are durable). Each entry: id, chat_id,
            description_preview, created_at, due_by.
        ``cursor_freshness`` — watched_chat_count, cursors_present,
            cursors_stale (older than 24 h), oldest_cursor_ts,
            newest_cursor_ts.
        ``watched_chat_count`` — count from the persisted watched_chats
            file (mirror of cursor_freshness.watched_chat_count for
            top-level convenience).
        ``generated_at`` — when the packet was assembled.
    """
    today_entries = _today_entries()
    return {
        "today_counts": _summarize_today(today_entries),
        "top_chats_today": _top_chats(today_entries),
        "open_promises": _open_promises_index(),
        "cursor_freshness": _cursor_freshness(),
        "watched_chat_count": _count_watched_chats(),
        "generated_at": _now_iso(),
    }
