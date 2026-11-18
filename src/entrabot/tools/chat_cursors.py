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
        "last_written_at": "2026-06-09T19:00:43.000Z",
        "reaction_keys_tail": [["msg-id-1", "user-id", "like", "<created>"], ...],  # optional
        "reaction_floor":     "2026-06-09T18:00:00.000+00:00"                      # optional
    }

``reaction_keys_tail`` and ``reaction_floor`` are the reaction ledger
(:class:`entrabot.tools.reactions.ReactionLedger`): which reactions on the agent's own
messages have already been noticed. It is separate state from ``seen_ids_tail`` and is only
changed by :func:`claim_reactions` and :func:`release_reactions`. A cursor without
``reaction_keys_tail`` has never had its reaction state built, which is different from an
empty list.

Every writer builds the stored cursor with :func:`_cursor_payload`, and every
read-modify-write goes through :func:`_update_cursor`.

Storage destination: ``LocalBackend`` by default; ``BlobBackend`` when blob
env vars are set. NEVER persona-sati — this is operational state, same bucket
as the watched-chat list and email cursor in :mod:`entrabot.tools.poll_state`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from urllib.parse import quote

from entrabot.storage.backend import get_backend
from entrabot.storage.blob import ConcurrencyError
from entrabot.tools.reactions import (
    REACTION_KEYS_FIELD as REACTION_KEYS_FIELD,  # re-exported: the cursor is this module's
)
from entrabot.tools.reactions import (
    ReactionKey,
    ReactionLedger,
    ReactionObservation,
    advance_reaction_ledger,
)

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
    The reaction ledger is written only when *state* has one.

    Backend write failures are propagated to the caller. Callers are
    expected to log + decide whether to retry — the poll-loop call sites
    (``mcp_server._chat_cursor_save`` / ``mcp_server._flush_chat_cursors``)
    already do this, so a single bad write doesn't take down the loop.
    """
    payload = _cursor_payload(
        last_ts=state.get("last_ts"),
        seen_ids=state.get("seen_ids_tail") or [],
        bootstrapped=state.get("bootstrapped", False),
        reactions=ReactionLedger.from_cursor(state),
    )
    backend = get_backend()
    backend.write_text(cursor_key(chat_id), json.dumps(payload))


def _cursor_payload(
    *,
    last_ts: str | None,
    seen_ids: Iterable[str],
    bootstrapped: object,
    reactions: ReactionLedger | None,
) -> dict:
    """The stored form of a chat cursor. The one place it is put together.

    ``seen_ids_tail`` is bounded and ``last_written_at`` is stamped here. The reaction
    ledger's fields are present only when there is a ledger: leaving them out is what tells
    a reader the reaction state was never built.
    """
    payload: dict = {
        "last_ts": last_ts,
        "seen_ids_tail": bound_seen_ids(seen_ids),
        "bootstrapped": bool(bootstrapped),
        "last_written_at": _now_iso(),
    }
    if reactions is not None:
        payload.update(reactions.to_cursor_fields())
    return payload


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


# Bounded retries for the ETag compare-and-swap in :func:`_update_cursor`.
# Each retry re-reads the shared cursor, so a handful covers realistic fleet
# write contention; exhausting them fails closed (claim nothing → push
# nothing) rather than risk a double delivery.
CLAIM_MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class _CursorStep[T]:
    """What one pass of a cursor update decided.

    ``result`` is what the caller gets back if this pass stands. ``payload`` is the cursor
    to write, or None to write nothing. ``create`` writes with ``if_absent`` instead of
    ``If-Match``, for a cursor the pass found did not exist.
    """

    result: T
    payload: dict | None = None
    create: bool = False


def _update_cursor[T](
    chat_id: str,
    step: Callable[[object, bool], _CursorStep[T] | None],
    *,
    operation: str,
    failed: T,
) -> T:
    """Read the shared cursor, let *step* decide, and write the result back conditionally.

    The one compare-and-swap loop for the chat cursor. Each pass reads the cursor with its
    ETag and calls ``step(parsed, exists)``, where *parsed* is the decoded JSON (any JSON
    value; None when there is no cursor) and *exists* says whether there is one. *step*
    returns a :class:`_CursorStep`, or None to give up.

    A write that loses the race (``ConcurrencyError``) re-reads and runs *step* again, up to
    :data:`CLAIM_MAX_ATTEMPTS` times. Everything ambiguous returns *failed*: a read that
    raises, a cursor that isn't JSON, *step* giving up, a write that raises, or retries
    running out. *operation* names the caller in the log.
    """
    backend = get_backend()
    key = cursor_key(chat_id)

    for _attempt in range(CLAIM_MAX_ATTEMPTS):
        try:
            raw, etag = backend.read_text_with_etag(key)
        except Exception as exc:  # noqa: BLE001 — ambiguous read → fail closed.
            logger.warning(
                "%s read failed for %s (fail closed, no push): %s: %s",
                operation,
                chat_id,
                type(exc).__name__,
                exc,
            )
            return failed

        parsed: object = None
        if raw is not None:
            try:
                parsed = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                logger.warning(
                    "%s: corrupt shared cursor for %s (fail closed)",
                    operation,
                    chat_id,
                )
                return failed

        outcome = step(parsed, raw is not None)
        if outcome is None:
            return failed
        if outcome.payload is None:
            return outcome.result

        try:
            if outcome.create:
                backend.write_text(key, json.dumps(outcome.payload), if_absent=True)
            else:
                backend.write_text(key, json.dumps(outcome.payload), if_match=etag or None)
        except ConcurrencyError:
            logger.info(
                "%s: cursor ETag conflict for %s; re-reading and retrying",
                operation,
                chat_id,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — write failure → fail closed.
            logger.warning(
                "%s write failed for %s (fail closed, no push): %s: %s",
                operation,
                chat_id,
                type(exc).__name__,
                exc,
            )
            return failed
        return outcome.result

    logger.warning(
        "%s: CAS retries exhausted for %s (fail closed, no push)",
        operation,
        chat_id,
    )
    return failed


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

    The reaction ledger is separate state: a message claim writes it back as it read it.
    """
    candidates = list(candidate_ids)
    if not candidates:
        return []

    def step(parsed: object, _exists: bool) -> _CursorStep[list[str]]:
        existing: dict = parsed if isinstance(parsed, dict) else {}
        seen = set(existing.get("seen_ids_tail") or [])
        newly = [mid for mid in candidates if mid not in seen]
        if not newly:
            return _CursorStep(result=[])  # every candidate already delivered by a sibling
        payload = _cursor_payload(
            last_ts=_later_ts(existing.get("last_ts"), last_ts),
            seen_ids=sorted(seen | set(candidates)),
            bootstrapped=True,
            reactions=ReactionLedger.from_cursor(existing),
        )
        return _CursorStep(result=newly, payload=payload)

    return _update_cursor(chat_id, step, operation="claim_delivery", failed=[])


@dataclass(frozen=True)
class ReactionClaim:
    """Result of :func:`claim_reactions`.

    ``resolved`` is False when storage gave no clear answer. Then nothing may be pushed, the
    caller keeps its in-memory ledger as it was, and the next poll cycle tries again.
    ``ledger`` is everything the shared cursor and the caller know between them. ``added``
    are the reactions this instance, and no other, should push.
    """

    resolved: bool
    ledger: ReactionLedger = ReactionLedger()
    added: tuple[ReactionKey, ...] = ()


def claim_reactions(
    chat_id: str,
    observation: ReactionObservation,
    *,
    known: ReactionLedger | None,
    base_state: dict,
    now: datetime | None = None,
) -> ReactionClaim:
    """Add *observation* to the shared cursor's reaction ledger and claim what is new.

    The reaction counterpart of :func:`claim_delivery`, with its own ledger. Across several
    instances polling one chat, exactly one wins the write for a given reaction.

    **A claim only adds.** The observation may come from an older read than the one a
    sibling already claimed from, so a reaction the caller didn't see is never taken out of
    the ledger. Removing entries is :func:`release_reactions`' job, by name, and age does
    the rest (:meth:`~entrabot.tools.reactions.ReactionLedger.trimmed`).

    * **Cold start.** If neither the stored cursor nor *known* (the ledger this process
      holds in memory) has a ledger, the observation becomes the ledger and nothing is
      claimed: reactions that predate the state are not news.
    * **Otherwise** the baseline is the stored ledger joined with *known*. *known* matters
      when something rewrote the cursor without its ledger: this process's memory still
      tells a new reaction from an old one.
    * **Nothing new.** Nothing is written.
    * **No cursor at all.** One is created from *base_state* (the caller's in-memory
      ``last_ts`` / ``seen_ids_tail`` / ``bootstrapped``), but only if it has a ``last_ts``:
      a cursor without a watermark would make the next restart replay the chat.

    Only the reaction ledger is changed. ``last_ts``, ``seen_ids_tail`` and ``bootstrapped``
    are written back as they were read.

    Fail-closed like :func:`claim_delivery`: a read failure, a corrupt cursor, a write failure
    or exhausted retries all return an unresolved claim with nothing to push. *now* is the
    clock entries are aged against; it defaults to the real one.
    """
    moment = now or datetime.now(UTC)

    def step(parsed: object, exists: bool) -> _CursorStep[ReactionClaim] | None:
        stored: ReactionLedger | None = None
        if not exists:
            if not base_state.get("last_ts"):
                logger.warning(
                    "claim_reactions: no cursor and no watermark for %s (fail closed)", chat_id
                )
                return None
            existing: dict = base_state
        elif isinstance(parsed, dict):
            existing = parsed
            stored = ReactionLedger.from_cursor(existing)
        else:
            logger.warning("claim_reactions: wrong-shaped cursor for %s (fail closed)", chat_id)
            return None

        if stored is None and known is None:
            baseline = None  # Cold start: the observation becomes the ledger, silently.
        else:
            baseline = (stored or ReactionLedger()).joined_with(known)
        update = advance_reaction_ledger(baseline, observation, now=moment)
        claim = ReactionClaim(resolved=True, ledger=update.ledger, added=update.added)
        if stored is not None and not update.added:
            return _CursorStep(result=claim)
        payload = _cursor_payload(
            last_ts=existing.get("last_ts"),
            seen_ids=existing.get("seen_ids_tail") or [],
            bootstrapped=existing.get("bootstrapped", False),
            reactions=update.ledger,
        )
        return _CursorStep(result=claim, payload=payload, create=not exists)

    return _update_cursor(
        chat_id, step, operation="claim_reactions", failed=ReactionClaim(resolved=False)
    )


def release_reactions(chat_id: str, keys: Iterable[ReactionKey]) -> bool:
    """Take exactly *keys* back out of the shared reaction ledger.

    For a claim whose push then failed: the reaction was never delivered, so it must not stay
    recorded as noticed. It is a removal of the named keys from whatever the ledger holds
    now, with the same compare-and-swap as a claim, so entries a sibling added meanwhile are
    kept. Nothing else in the cursor changes.

    Returns True when the keys are no longer in the stored ledger (including when there was
    no cursor or no ledger to begin with), and False when storage gave no clear answer.
    """
    released = frozenset(keys)
    if not released:
        return True

    def step(parsed: object, exists: bool) -> _CursorStep[bool] | None:
        if not exists:
            return _CursorStep(result=True)
        if not isinstance(parsed, dict):
            return None
        stored = ReactionLedger.from_cursor(parsed)
        if stored is None or not (stored.keys & released):
            return _CursorStep(result=True)
        payload = _cursor_payload(
            last_ts=parsed.get("last_ts"),
            seen_ids=parsed.get("seen_ids_tail") or [],
            bootstrapped=parsed.get("bootstrapped", False),
            reactions=stored.without(released),
        )
        return _CursorStep(result=True, payload=payload)

    return _update_cursor(chat_id, step, operation="release_reactions", failed=False)


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
