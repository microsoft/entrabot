"""Poll state that follows the configured ``MemoryBackend``.

Two values decide what the background polls deliver: the list of chats to watch
and the email cursor. Both used to be plain files under ``cfg.data_dir`` even in
cloud mode, so a machine moved to another agent's container kept the old agent's
chat list and a months-old cursor, and two machines on one container shared
neither. This module routes both through :func:`get_backend` instead (design
F3 in ``DESIGN-multi-instance-cursor-consistency.md``).

Keys:

* ``watched_chats`` holds one chat ID per line, sorted. In local mode this is the
  same file as before, so local users have nothing to migrate.
* ``poll_state/email_cursor.txt`` holds one RFC 3339 timestamp. The key is new on
  purpose. Existing cloud containers hold a stale ``email_cursor.txt`` that setup's
  migration copied once and nothing ever updated, because runtime never read it.
  Reading that key would replay up to a full page of old mail on upgrade, and the
  design doc explains why replayed imperative messages are a security problem. So
  a cloud backend starts this cursor at "now" and consults neither the old key nor
  any local file. Mail that arrives while no instance runs isn't pushed, but stays
  readable through the ``read_email`` tool.

The rules that keep this fleet-safe:

* Reads classify as PRESENT / ABSENT / UNRESOLVED, like ``chat_cursors``. A failed
  read is never treated as absent.
* Every write is a compare-and-swap, including the first one, so adding a chat
  can't remove another instance's chats and exactly one instance can claim a given
  window of mail. A value that came back without an ETag can't be swapped safely,
  so it is treated as a failure rather than overwritten.
* Nothing here raises for a storage failure. Callers get an outcome to act on,
  which keeps a storage blip from taking down a poll loop.

All of these calls can do blocking network I/O, so async callers must run them in
a worker thread. A past bug starved the MCP ``initialize`` handshake this way.

``.singleton.lock`` deliberately stays local: it is a per-machine process lock.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from entrabot.config import get_config
from entrabot.storage.backend import LocalBackend, MemoryBackend, get_backend
from entrabot.storage.blob import ConcurrencyError

logger = logging.getLogger("entrabot.tools.poll_state")

WATCHED_CHATS_KEY = "watched_chats"
EMAIL_CURSOR_KEY = "poll_state/email_cursor.txt"
LEGACY_EMAIL_CURSOR_KEY = "email_cursor.txt"

# Bounded compare-and-swap retries, matching chat_cursors.CLAIM_MAX_ATTEMPTS. Each
# retry re-reads the list, so a handful covers realistic fleet contention. Running
# out reports failure; the caller keeps the chats dirty and retries later.
WATCHED_CHATS_MAX_ATTEMPTS = 4

# Characters that can't appear in a Teams chat ID but could change the meaning of
# the Graph URL each ID is interpolated into. The list lives in shared storage, so
# it is parsed as data. This only rejects what is unsafe; whether a chat is
# trusted is decided elsewhere, by Graph membership (SponsorGate).
_UNSAFE_CHAT_ID_CHARS = re.compile(r"[\s/\\?#%\x00-\x1f\x7f]")

_LEGACY_LOCAL_FILES = (WATCHED_CHATS_KEY, LEGACY_EMAIL_CURSOR_KEY)


class PollStateOutcome(Enum):
    """Classification of a poll-state read, mirroring ``chat_cursors.CursorOutcome``.

    * ``ABSENT``: the read succeeded and the key doesn't exist.
    * ``PRESENT``: the key exists and its value is usable.
    * ``UNRESOLVED``: the read failed or the value is unusable. This is ambiguous,
      since a live value may be hiding behind a transient failure, so the caller
      must not act as if the key were absent.
    """

    ABSENT = "absent"
    PRESENT = "present"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class WatchedChatsRead:
    """The shared watched-chat list. ``chat_ids`` is empty unless ``PRESENT``."""

    outcome: PollStateOutcome
    chat_ids: tuple[str, ...] = ()
    etag: str | None = None


@dataclass(frozen=True)
class WatchedChatsWrite:
    """Result of :func:`add_watched_chats`.

    ``persisted`` means every requested chat is now in the stored list. ``added``
    holds the ones this call newly wrote, so it is empty when they were all there
    already. When ``persisted`` is false the caller should retry later.
    """

    persisted: bool
    added: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailCursorRead:
    """The shared email cursor. ``cursor`` and ``etag`` are set only when ``PRESENT``."""

    outcome: PollStateOutcome
    cursor: str | None = None
    etag: str | None = None


class EmailClaim(Enum):
    """Outcome of trying to claim a window of mail with a compare-and-swap.

    * ``WON``: this instance advanced the cursor and must push the batch.
    * ``LOST``: another instance claimed this window first. Drop the batch.
    * ``FAILED``: the write failed for another reason. Push nothing (fail closed);
      the cursor didn't move, so the next cycle fetches the same window again.
    """

    WON = "won"
    LOST = "lost"
    FAILED = "failed"


def is_chat_id(value: str) -> bool:
    """True when *value* is safe to treat as a chat ID (see the note on the regex)."""
    return bool(value) and not _UNSAFE_CHAT_ID_CHARS.search(value)


def _parse_watched_chats(raw: str) -> tuple[str, ...]:
    """Parse the newline format, dropping blanks, duplicates and malformed lines."""
    chat_ids: dict[str, None] = {}
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if is_chat_id(candidate):
            chat_ids[candidate] = None
        else:
            # Report only the length: the line is untrusted and could be anything.
            logger.warning(
                "Ignoring malformed entry in the watched-chat list (%d chars)",
                len(candidate),
            )
    return tuple(chat_ids)


def _format_watched_chats(chat_ids: Iterable[str]) -> str:
    return "\n".join(sorted(chat_ids)) + "\n"


def _read_watched_chats_from(backend: MemoryBackend) -> WatchedChatsRead:
    raw, etag = backend.read_text_with_etag(WATCHED_CHATS_KEY)
    if raw is None:
        return WatchedChatsRead(PollStateOutcome.ABSENT)
    return WatchedChatsRead(PollStateOutcome.PRESENT, _parse_watched_chats(raw), etag)


def read_watched_chats() -> WatchedChatsRead:
    """Read the watched-chat list. A read failure is UNRESOLVED, never ABSENT."""
    try:
        return _read_watched_chats_from(get_backend())
    except Exception as exc:  # noqa: BLE001 — any read failure is ambiguous.
        logger.warning(
            "Watched-chat list read failed (UNRESOLVED): %s: %s",
            type(exc).__name__,
            exc,
        )
        return WatchedChatsRead(PollStateOutcome.UNRESOLVED)


def add_watched_chats(chat_ids: Iterable[str]) -> WatchedChatsWrite:
    """Merge *chat_ids* into the shared list with a bounded compare-and-swap.

    Reads the list with its ETag, writes the union back with ``if_match``, and on
    a conflict re-reads and tries again, so adding never removes a chat another
    instance added. Takes a batch so a discovery sweep costs one write. Writes
    nothing when every chat is already listed, and nothing after a failed read,
    which must never be mistaken for an empty list. Never raises.
    """
    requested = {chat_id for chat_id in chat_ids if is_chat_id(chat_id)}
    if not requested:
        return WatchedChatsWrite(persisted=True)

    try:
        backend = get_backend()
        for _attempt in range(WATCHED_CHATS_MAX_ATTEMPTS):
            current = _read_watched_chats_from(backend)
            new = requested.difference(current.chat_ids)
            if not new:
                return WatchedChatsWrite(persisted=True)

            content = _format_watched_chats(new.union(current.chat_ids))
            try:
                if current.outcome is PollStateOutcome.ABSENT:
                    backend.write_text(WATCHED_CHATS_KEY, content, if_absent=True)
                elif current.etag:
                    backend.write_text(WATCHED_CHATS_KEY, content, if_match=current.etag)
                else:
                    logger.warning(
                        "Watched-chat list has no ETag; refusing an unconditional overwrite"
                    )
                    return WatchedChatsWrite(persisted=False)
            except ConcurrencyError:
                logger.info("Watched-chat list changed during update; re-reading and retrying")
                continue
            return WatchedChatsWrite(persisted=True, added=tuple(sorted(new)))
    except Exception as exc:  # noqa: BLE001 — report failure; the caller retries later.
        logger.warning(
            "Watched-chat list update failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return WatchedChatsWrite(persisted=False)

    logger.warning(
        "Watched-chat list update gave up after %d conflicting attempts",
        WATCHED_CHATS_MAX_ATTEMPTS,
    )
    return WatchedChatsWrite(persisted=False)


def _now_cursor() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_timestamp(value: str) -> bool:
    """True when *value* parses as a timestamp.

    The cursor is interpolated into a Graph ``$filter``, so this also keeps a
    tampered shared value from changing the query.
    """
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def read_email_cursor() -> EmailCursorRead:
    """Read the email cursor. Never consults the old key or any local file.

    A failed read, a value that isn't a timestamp, and a value with no ETag are all
    UNRESOLVED. The last one matters because an ETag is what lets the cursor be
    claimed, and an unclaimable window must not be pushed.
    """
    try:
        raw, etag = get_backend().read_text_with_etag(EMAIL_CURSOR_KEY)
    except Exception as exc:  # noqa: BLE001 — any read failure is ambiguous.
        logger.warning(
            "Email cursor read failed (UNRESOLVED): %s: %s",
            type(exc).__name__,
            exc,
        )
        return EmailCursorRead(PollStateOutcome.UNRESOLVED)

    if raw is None:
        return EmailCursorRead(PollStateOutcome.ABSENT)

    cursor = raw.strip()
    if not _is_timestamp(cursor):
        logger.warning(
            "Email cursor at %s is not a timestamp (UNRESOLVED). Delete the key to "
            "restart the cursor at now.",
            EMAIL_CURSOR_KEY,
        )
        return EmailCursorRead(PollStateOutcome.UNRESOLVED)
    if not etag:
        logger.warning("Email cursor has no ETag and can't be claimed (UNRESOLVED)")
        return EmailCursorRead(PollStateOutcome.UNRESOLVED)
    return EmailCursorRead(PollStateOutcome.PRESENT, cursor, etag)


def initialize_email_cursor(now: str | None = None) -> bool:
    """Create the cursor key, returning True once it exists.

    Cloud backends start at *now* (defaulting to the current time). Only a
    ``LocalBackend`` may adopt the legacy ``email_cursor.txt``, so local-only users
    have no gap after upgrading; that file stays in place and is never consulted
    again once the new key exists. Any other backend is treated as cloud, so an
    unrecognized one can't reach for old state.

    Creation is create-only, so losing a race to another instance keeps the winner's
    value and still returns True. False means the key couldn't be created this time;
    it stays absent and the next cycle tries again.
    """
    value = now or _now_cursor()
    try:
        backend = get_backend()
        if isinstance(backend, LocalBackend):
            legacy = (backend.read_text(LEGACY_EMAIL_CURSOR_KEY) or "").strip()
            if _is_timestamp(legacy):
                value = legacy
                logger.info("Adopted legacy local email cursor (%s)", value)
            elif legacy:
                logger.warning("Ignoring legacy local email cursor: not a timestamp")
        backend.write_text(EMAIL_CURSOR_KEY, value, if_absent=True)
    except ConcurrencyError:
        logger.info("Email cursor was already initialized by another instance")
        return True
    except Exception as exc:  # noqa: BLE001 — stay absent; the next cycle retries.
        logger.warning(
            "Email cursor initialization failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return False
    logger.info("Initialized email cursor at %s", value)
    return True


def claim_email_window(new_cursor: str, etag: str | None) -> EmailClaim:
    """Advance the cursor from the value read with *etag*, claiming that window.

    Call this BEFORE pushing anything: only the instance that wins this write owns
    the batch. *etag* must come from the read the batch was fetched with. A missing
    ETag fails closed instead of degrading to an unconditional write.
    """
    if not etag:
        logger.warning("Email window claim has no ETag; refusing an unconditional write")
        return EmailClaim.FAILED
    try:
        get_backend().write_text(EMAIL_CURSOR_KEY, new_cursor.strip(), if_match=etag)
    except ConcurrencyError:
        return EmailClaim.LOST
    except Exception as exc:  # noqa: BLE001 — fail closed: push nothing, retry next cycle.
        logger.warning(
            "Email cursor write failed (pushing nothing): %s: %s",
            type(exc).__name__,
            exc,
        )
        return EmailClaim.FAILED
    return EmailClaim.WON


def ignored_legacy_local_copies() -> list[Path]:
    """Return legacy local poll-state files that cloud mode is ignoring.

    Empty in local mode, where those files are in use. Only checks that the files
    exist, for the one startup log line; their content is never read.
    """
    try:
        if isinstance(get_backend(), LocalBackend):
            return []
        data_dir = get_config().data_dir
        return [path for name in _LEGACY_LOCAL_FILES if (path := data_dir / name).is_file()]
    except Exception as exc:  # noqa: BLE001 — this only feeds an informational log line.
        logger.debug("Could not check for legacy local poll state: %s", exc)
        return []
