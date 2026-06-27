"""Per-chat Teams poll cursor — persisted through ``MemoryBackend``.

Solves GitHub issue #17: the background Teams poll's per-chat cursor
(``last_ts``, ``seen_ids``, ``bootstrapped``) used to live in-process at
``_state["watched_chats"][chat_id]`` only. On every MCP restart the bootstrap
path re-surfaced "the newest message at boot" as if it were fresh — even when
that newest message was days old — and messages that arrived during a server-
down window were silently dropped.

This module persists the cursor through the same ``MemoryBackend`` protocol
used by ``interaction_log.py`` / ``daily_summary.py`` (ADR-005 Phase 2). One
key per chat (``chat_cursors/<chat_id>.json``) so writes to a busy chat don't
rewrite a giant blob.

Same shape as :mod:`entrabot.tools.email_poll` — parallel architecture, same
problem class, already-solved-once.

Cursor schema (per chat):

    {
        "last_ts":         "2026-06-09T18:59:15.261Z",
        "seen_ids_tail":   ["msg-id-1", "msg-id-2", ...],   # ~50 most recent
        "bootstrapped":    true,
        "last_written_at": "2026-06-09T19:00:43.000Z"
    }

Storage destination: ``LocalBackend`` by default; ``BlobBackend`` when blob
env vars are set. NEVER persona-sati — this is operational state, same bucket
as ``watched_chats`` and ``email_cursor.txt``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import quote

from entrabot.storage.backend import get_backend

logger = logging.getLogger("entrabot.tools.chat_cursors")

# Per-chat seen-id ring bound. ``last_ts`` carries everything older — the tail
# only needs to cover the 2-second overlap window the poll uses to defend
# against same-second message races. 50 is plenty.
MAX_SEEN_IDS_TAIL = 50

# Staleness cap: if the cursor's ``last_written_at`` (when it was last
# persisted) is older than this, the server was likely down long enough that
# messages may have been missed and the seen-set can't be trusted — re-baseline
# via a fresh bootstrap. NOTE: measured from the cursor's WRITE time, not from
# ``last_ts`` (the newest-message watermark). Keying off ``last_ts`` re-fired
# every idle chat's old newest message on each restart — the replay flood.
CURSOR_STALENESS_SECONDS = 24 * 60 * 60  # 24 hours

# Storage key prefix. One file per chat under this prefix so writes are
# independent — a busy chat doesn't trigger a giant blob rewrite.
_CURSOR_KEY_PREFIX = "chat_cursors/"


def cursor_key(chat_id: str) -> str:
    """Return the backend key for *chat_id*'s cursor file.

    The chat_id is URL-quoted so Teams thread IDs containing ``:`` and ``@``
    survive both LocalBackend (filesystem) and BlobBackend (blob name)
    constraints without ambiguity.
    """
    return f"{_CURSOR_KEY_PREFIX}{quote(chat_id, safe='')}.json"


def bound_seen_ids(seen_ids: Iterable[str]) -> list[str]:
    """Bound *seen_ids* to the last :data:`MAX_SEEN_IDS_TAIL` entries.

    Accepts a set (the shape ``_state["watched_chats"][chat_id]["seen_ids"]``
    has) or a list; preserves order when input is ordered, otherwise the
    output is unordered but bounded. ``last_ts`` carries everything older.
    """
    as_list = list(seen_ids)
    if len(as_list) <= MAX_SEEN_IDS_TAIL:
        return as_list
    return as_list[-MAX_SEEN_IDS_TAIL:]


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def load_cursor(chat_id: str) -> dict | None:
    """Return the persisted cursor for *chat_id*, or ``None`` if absent/corrupt.

    Corrupt JSON is treated as "not present" rather than raising — boot must
    not die because a single chat's cursor file got truncated. The caller
    will fall through to the bootstrap path.
    """
    backend = get_backend()
    raw = backend.read_text(cursor_key(chat_id))
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Corrupt chat cursor for %s (treating as absent): %s",
            chat_id,
            exc,
        )
        return None
    if not isinstance(parsed, dict):
        logger.warning(
            "Unexpected chat cursor shape for %s (treating as absent): %r",
            chat_id,
            type(parsed),
        )
        return None
    return parsed


def save_cursor(chat_id: str, state: dict) -> None:
    """Persist *state* as *chat_id*'s cursor through the configured backend.

    ``seen_ids_tail`` is bounded to :data:`MAX_SEEN_IDS_TAIL` on write so the
    serialized payload stays small even after a long-lived poll session.
    ``last_written_at`` is stamped here so callers don't have to track it.

    Backend write failures are propagated to the caller. Callers are
    expected to log + decide whether to retry — the poll-loop call sites
    (``mcp_server._chat_cursor_save`` / ``mcp_server._flush_chat_cursors``)
    already do this, so a single bad write doesn't take down the loop.
    """
    payload = {
        "last_ts": state.get("last_ts"),
        "seen_ids_tail": bound_seen_ids(state.get("seen_ids_tail") or []),
        "bootstrapped": bool(state.get("bootstrapped", False)),
        "last_written_at": _now_iso(),
    }
    backend = get_backend()
    backend.write_text(cursor_key(chat_id), json.dumps(payload))


def is_stale(last_written_at: str | None) -> bool:
    """Return True if a cursor written at *last_written_at* is too old to trust.

    Staleness is measured from ``last_written_at`` — *when the cursor was
    last persisted* — NOT from ``last_ts``, the newest *message* watermark.
    This distinction is the whole fix: an idle chat legitimately has an old
    ``last_ts`` (its newest message may be weeks old) while its cursor was
    written seconds ago. Such a cursor is perfectly trustworthy — rehydrating
    it preserves the seen-set and the watermark, so the old message is NOT
    re-surfaced.

    The prior implementation keyed off ``last_ts``, so every chat idle longer
    than the cap was judged "stale" and re-bootstrapped on each restart —
    and ``_bootstrap_chat`` deliberately leaves the newest message unseen, so
    that weeks-old message got re-pushed as if it were live. With ~50 idle
    chats and frequent restarts that produced a flood of stale replays.

    Keying off ``last_written_at`` preserves the genuine protection the cap is
    for: if the server was actually down longer than
    :data:`CURSOR_STALENESS_SECONDS`, messages may have been missed and the
    seen-set can no longer be trusted, so we re-baseline via a fresh bootstrap.

    ``None``, empty string, and unparseable timestamps are treated as stale
    (defensive: better to bootstrap than to crash boot on a bad cursor).
    """
    if not last_written_at:
        return True
    try:
        dt = datetime.fromisoformat(last_written_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - dt).total_seconds()
    return age > CURSOR_STALENESS_SECONDS
