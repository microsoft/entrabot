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
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from urllib.parse import quote

from entrabot.storage.backend import get_backend
from entrabot.storage.blob import ConcurrencyError

logger = logging.getLogger("entrabot.tools.chat_cursors")

# Per-chat seen-id ring bound. ``last_ts`` carries everything older — the tail
# only needs to cover the 2-second overlap window the poll uses to defend
# against same-second message races. 50 is plenty.
MAX_SEEN_IDS_TAIL = 50

# Staleness cap: if the persisted cursor's ``last_ts`` is older than this,
# treat the chat as needing a fresh bootstrap. Better to bootstrap than to
# fire a 3-day-old message as if it were live (the symptom that drove this
# fix — today's session replayed messages from 11 days ago).
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


class CursorOutcome(Enum):
    """Classification of a cursor read, used to decide whether the poll may push.

    The distinction is load-bearing for fleet safety (design doc F1):

    * ``ABSENT`` — the read SUCCEEDED and no cursor exists. This is the only
      case where "surface the newest message once" is allowed: a genuinely new
      chat that no instance has ever cursor-ed.
    * ``PRESENT`` — a cursor exists and parsed (fresh OR stale). Always
      rehydrate; the steady-state timestamp gate does catch-up (F4). Never
      re-bootstrap a present cursor.
    * ``UNRESOLVED`` — the read FAILED, or the payload is corrupt / the wrong
      shape. Ambiguous: a transient blob 401/timeout/throttle or a partial
      write could be hiding a live cursor. **Fail closed — never push.** The
      caller must retry the read on a later cycle before delivering anything.
    """

    ABSENT = "absent"
    PRESENT = "present"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class CursorResolution:
    """Result of :func:`resolve_cursor` — an outcome plus the parsed cursor.

    ``cursor`` is populated only for :attr:`CursorOutcome.PRESENT`; it is
    ``None`` for ``ABSENT`` and ``UNRESOLVED``.
    """

    outcome: CursorOutcome
    cursor: dict | None


def resolve_cursor(chat_id: str) -> CursorResolution:
    """Classify *chat_id*'s cursor read into ABSENT / PRESENT / UNRESOLVED.

    Unlike :func:`load_cursor` (which collapses every miss to ``None``), this
    keeps the three cases apart so the poll can fail closed. The rules:

    * Backend read raises (transient blob/disk error, 401 refresh race,
      throttle) → ``UNRESOLVED``. The whole point of the fleet-safety fix: an
      ambiguous read must never be mistaken for "new chat, push newest".
    * Read returns ``None`` (the backend positively determined the key is
      absent) → ``ABSENT``.
    * Content present but not valid JSON, or valid JSON that isn't an object →
      ``UNRESOLVED``. A partial/corrupt write is ambiguous; do not treat it as
      a clean slate and re-push.
    * Content present and a JSON object (fresh OR stale) → ``PRESENT``.
    """
    backend = get_backend()
    try:
        raw = backend.read_text(cursor_key(chat_id))
    except Exception as exc:  # noqa: BLE001 — any read failure is ambiguous.
        logger.warning(
            "Cursor read failed for %s (UNRESOLVED, failing closed): %s: %s",
            chat_id,
            type(exc).__name__,
            exc,
        )
        return CursorResolution(CursorOutcome.UNRESOLVED, None)

    if raw is None:
        return CursorResolution(CursorOutcome.ABSENT, None)

    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Corrupt chat cursor for %s (UNRESOLVED, failing closed): %s",
            chat_id,
            exc,
        )
        return CursorResolution(CursorOutcome.UNRESOLVED, None)

    if not isinstance(parsed, dict):
        logger.warning(
            "Unexpected chat cursor shape for %s (UNRESOLVED, failing closed): %r",
            chat_id,
            type(parsed),
        )
        return CursorResolution(CursorOutcome.UNRESOLVED, None)

    return CursorResolution(CursorOutcome.PRESENT, parsed)


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


def _later_ts(a: str | None, b: str | None) -> str | None:
    """Return the later of two ISO-8601 timestamps, ignoring ``None``.

    Parses to timezone-aware datetimes so mixed sub-second precision compares
    correctly (``"...15Z"`` vs ``"...15.261Z"``, where a naive string ``max``
    would pick the wrong one). Falls back to string ``max`` if either value is
    unparseable — defensive, never raises.
    """
    if a is None:
        return b
    if b is None:
        return a

    def _parse(ts: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    da, db = _parse(a), _parse(b)
    if da is None or db is None:
        return max(a, b)
    return a if da >= db else b


# Bounded retries for the ETag compare-and-swap in :func:`claim_delivery`.
# Each retry re-reads the shared cursor, so a handful covers realistic fleet
# write contention; exhausting them fails closed (claim nothing → push
# nothing) rather than risk a double delivery.
CLAIM_MAX_ATTEMPTS = 4


def claim_delivery(
    chat_id: str,
    candidate_ids: Iterable[str],
    last_ts: str | None = None,
) -> list[str]:
    """Atomically claim delivery of *candidate_ids* in the shared cloud cursor.

    The persisted cursor's ``seen_ids_tail`` doubles as the fleet delivery
    ledger. This reads the shared cursor, computes which candidates are NOT
    already recorded as delivered, writes the merged cursor
    (``seen ∪ candidates``, ``max(last_ts)``) back with an ``If-Match``
    precondition, and returns the newly-claimed ids — the ones THIS instance
    should push.

    Fleet guarantee (design "per-message cloud idempotency" + F5): across N
    instances polling the same message, exactly one wins the compare-and-swap
    for a given id; the losers hit ``ConcurrencyError``, re-read, see the id
    already delivered, and claim nothing for it. Delivery is idempotent across
    the whole fleet.

    Fail-closed everywhere: a read failure, a corrupt shared cursor, or
    exhausted CAS retries all return ``[]`` (claim nothing → push nothing)
    rather than risk re-injecting a message.
    """
    candidates = list(candidate_ids)
    if not candidates:
        return []

    backend = get_backend()
    key = cursor_key(chat_id)

    for _attempt in range(CLAIM_MAX_ATTEMPTS):
        try:
            raw, etag = backend.read_text_with_etag(key)
        except Exception as exc:  # noqa: BLE001 — ambiguous read → fail closed.
            logger.warning(
                "claim_delivery read failed for %s (fail closed, no push): %s: %s",
                chat_id,
                type(exc).__name__,
                exc,
            )
            return []

        existing: dict = {}
        if raw is not None:
            try:
                parsed = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                logger.warning(
                    "claim_delivery: corrupt shared cursor for %s (fail closed)",
                    chat_id,
                )
                return []
            if isinstance(parsed, dict):
                existing = parsed

        seen = set(existing.get("seen_ids_tail") or [])
        newly = [mid for mid in candidates if mid not in seen]
        if not newly:
            return []  # every candidate already delivered by a sibling

        merged_seen = seen | set(candidates)
        payload = {
            "last_ts": _later_ts(existing.get("last_ts"), last_ts),
            "seen_ids_tail": bound_seen_ids(sorted(merged_seen)),
            "bootstrapped": True,
            "last_written_at": _now_iso(),
        }

        try:
            backend.write_text(key, json.dumps(payload), if_match=etag or None)
        except ConcurrencyError:
            logger.info(
                "claim_delivery: cursor ETag conflict for %s; re-reading and retrying",
                chat_id,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — write failure → fail closed.
            logger.warning(
                "claim_delivery write failed for %s (fail closed, no push): %s: %s",
                chat_id,
                type(exc).__name__,
                exc,
            )
            return []
        return newly

    logger.warning(
        "claim_delivery: CAS retries exhausted for %s (fail closed, no push)",
        chat_id,
    )
    return []


def is_stale(last_ts: str | None) -> bool:
    """Return True if *last_ts* is too old to safely rehydrate from.

    "Too old" means older than :data:`CURSOR_STALENESS_SECONDS`. A stale
    cursor triggers a fresh ``_bootstrap_chat`` instead of rehydration — this
    is the defense against the 11-day-old replay flood that motivated this
    fix.

    ``None``, empty string, and unparseable timestamps are treated as stale
    (defensive: better to bootstrap than to crash boot on a bad cursor).
    """
    if not last_ts:
        return True
    try:
        dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - dt).total_seconds()
    return age > CURSOR_STALENESS_SECONDS
