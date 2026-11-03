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
* ``poll_state/email_cursor.txt`` holds one RFC 3339 timestamp, followed by the IDs
  of the most recent claims, newest first (see :func:`claim_email_window`). The key is new on
  purpose. Existing cloud containers hold a stale ``email_cursor.txt`` that setup's
  migration copied once and nothing ever updated, because runtime never read it.
  Reading that key would replay up to a full page of old mail on upgrade, and the
  design doc explains why replayed imperative messages are a security problem. So
  a cloud backend starts this cursor at "now" and consults neither the old key nor
  any local file. That happens only when the key is first initialized: mail from
  before then isn't pushed, but stays readable through the ``read_email`` tool. Once
  the key exists a restart reuses it, and qualifying mail received while no instance
  ran is fetched and pushed as normal.

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
import uuid
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
# A single-chat key from before chats were explicit. Nothing writes it any more, so a
# copy is only ever as old as the install that held it.
LEGACY_DEFAULT_CHAT_KEY = "chat_id"

# Bounded compare-and-swap retries, matching chat_cursors.CLAIM_MAX_ATTEMPTS. Each
# retry re-reads the list, so a handful covers realistic fleet contention. Running
# out reports failure; the caller keeps the chats dirty and retries later.
WATCHED_CHATS_MAX_ATTEMPTS = 4

# How many claim IDs the cursor value keeps, newest first. A claim whose write was
# ambiguous is settled later by finding its ID here, so the history has to outlast
# whatever other instances claim in the meantime. Each claim is a fresh 32-character ID,
# so the value stays around a kilobyte. A claim followed by more than this many others
# can no longer be found, which :func:`settle_email_claim` reports as "did not land".
MAX_RECENT_CLAIMS = 32

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
    """The shared email cursor.

    ``cursor`` and ``etag`` are set only when ``PRESENT``. ``recent_claims`` holds the
    IDs of the latest claims stored with the cursor, newest first, and is empty for a
    plain cursor. A caller passes it back as ``prior_claims`` when it claims the next
    window, so the history carries forward.

    ``corrupt`` is True only when the read succeeded and the stored value is unusable (it
    isn't a timestamp). That is where deleting the key is the way out. A failed read says
    nothing about what the key holds, so it is never ``corrupt``.
    """

    outcome: PollStateOutcome
    cursor: str | None = None
    etag: str | None = None
    recent_claims: tuple[str, ...] = ()
    corrupt: bool = False


class EmailClaim(Enum):
    """Outcome of trying to claim a window of mail with a compare-and-swap.

    * ``WON``: this instance advanced the cursor and must push the batch.
    * ``LOST``: another write landed first, so this one can no longer commit. Drop the
      batch; whoever's write it was owns the window.
    * ``FAILED``: there was no ETag, so no write was attempted. Push nothing (fail
      closed); the cursor didn't move, so the next cycle fetches the same window again.
    * ``UNKNOWN``: the write failed and :func:`settle_email_claim` couldn't say how the
      claim came out: the cursor couldn't be read, wasn't usable, or the retried write
      failed too. If the claim did land, the cursor has advanced and the batch will never
      be fetched again, so dropping it could lose mail. Push nothing, but hold the batch
      and settle it later with :func:`settle_email_claim`.
    """

    WON = "won"
    LOST = "lost"
    FAILED = "failed"
    UNKNOWN = "unknown"


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


def now_cursor() -> str:
    """The current time as a cursor, to the millisecond.

    Whole seconds would round down, so mail received earlier in the same second would
    be newer than the cursor and a first boot could replay it. This is the format
    :func:`entrabot.tools.email_poll.advance_cursor` produces
    (``%Y-%m-%dT%H:%M:%S.mmmZ``), so a fresh cursor and an advanced one look alike.

    Public so a caller whose first attempt to create the cursor fails can read the
    clock once and hand the same value to every retry (see
    :func:`initialize_email_cursor`).
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _is_timestamp(value: str) -> bool:
    """True when *value* is a complete RFC 3339 date-time with a timezone.

    The cursor is interpolated into a Graph ``$filter``, so this also keeps a tampered
    shared value from changing the query. A bare date parses but can't be used there,
    and accepting one would leave every poll failing instead of reporting it.
    """
    if "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _cursor_line(raw: str) -> str:
    """The timestamp from a stored cursor. Any lines after it are claim IDs."""
    lines = raw.strip().splitlines()
    return lines[0].strip() if lines else ""


def _claim_ids(raw: str) -> tuple[str, ...]:
    """The claim IDs stored after the timestamp: the non-blank later lines, in order.

    A cursor with no such lines, like the one initialization writes, has none.
    """
    return tuple(line.strip() for line in raw.strip().splitlines()[1:] if line.strip())


def read_email_cursor() -> EmailCursorRead:
    """Read the email cursor. Never consults the old key or any local file.

    A failed read, a value that isn't a timestamp, and a value with no ETag are all
    UNRESOLVED. The last one matters because an ETag is what lets the cursor be
    claimed, and an unclaimable window must not be pushed. Only the value that isn't a
    timestamp is also ``corrupt``: the read worked and the key holds something unusable.
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

    cursor = _cursor_line(raw)
    if not _is_timestamp(cursor):
        logger.warning(
            "Email cursor at %s is not a timestamp (UNRESOLVED). Delete the key to "
            "restart the cursor at now.",
            EMAIL_CURSOR_KEY,
        )
        return EmailCursorRead(PollStateOutcome.UNRESOLVED, corrupt=True)
    if not etag:
        logger.warning("Email cursor has no ETag and can't be claimed (UNRESOLVED)")
        return EmailCursorRead(PollStateOutcome.UNRESOLVED)
    return EmailCursorRead(PollStateOutcome.PRESENT, cursor, etag, _claim_ids(raw))


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

    A caller that tries again after False should pass the same *now* each time, read
    once from :func:`now_cursor`. With the default, every attempt reads its own clock,
    so the cursor would start at the attempt that finally worked and skip the mail that
    arrived while creation was failing.
    """
    value = now or now_cursor()
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


def new_claim_id() -> str:
    """A fresh ID for one claim of a window of mail.

    The caller makes it, rather than :func:`claim_email_window`, so that it still has the
    ID when the claim comes back ``UNKNOWN`` and has to be settled later.
    """
    return uuid.uuid4().hex


def claim_email_window(
    new_cursor: str,
    etag: str | None,
    *,
    claim_id: str,
    prior_claims: tuple[str, ...] = (),
) -> EmailClaim:
    """Advance the cursor from the value read with *etag*, claiming that window.

    Call this BEFORE pushing anything: only the instance that wins this write owns
    the batch. *etag* must come from the read the batch was fetched with. A missing
    ETag fails closed instead of degrading to an unconditional write.

    The stored value is the new cursor, then *claim_id*, then *prior_claims* (the
    ``recent_claims`` of the read that supplied *etag*), keeping at most
    :data:`MAX_RECENT_CLAIMS` IDs in total, newest first. Take *claim_id* from
    :func:`new_claim_id` and keep it.

    An error doesn't prove the write failed. Azure may have committed it before the
    response was lost, and a request that timed out can still commit after this gives up
    on it. Dropping the batch then would lose it, because the next poll would read the
    advanced cursor and never fetch that mail. So after ``ConcurrencyError`` or any other
    exception this settles the claim with :func:`settle_email_claim`, called with the same
    arguments: True is WON, False is LOST and None is UNKNOWN. Hold the batch on UNKNOWN,
    and call :func:`settle_email_claim` again until storage answers.
    """
    if not etag:
        logger.warning("Email window claim has no ETag; refusing an unconditional write")
        return EmailClaim.FAILED
    value = _claim_value(new_cursor, claim_id, prior_claims)
    try:
        get_backend().write_text(EMAIL_CURSOR_KEY, value, if_match=etag)
    except Exception as exc:  # noqa: BLE001 — an error doesn't say whether the write landed.
        settled = settle_email_claim(new_cursor, etag, claim_id=claim_id, prior_claims=prior_claims)
        if settled is None:
            logger.warning(
                "Email cursor write failed (%s) and the claim can't be settled yet; "
                "the claim is unknown (holding the batch)",
                type(exc).__name__,
            )
            return EmailClaim.UNKNOWN
        if settled:
            logger.info(
                "Email cursor write reported %s but the claim is ours; keeping it",
                type(exc).__name__,
            )
            return EmailClaim.WON
        logger.info(
            "Email cursor write reported %s and another write landed first; dropping the claim",
            type(exc).__name__,
        )
        return EmailClaim.LOST
    return EmailClaim.WON


def _claim_value(new_cursor: str, claim_id: str, prior_claims: Iterable[str]) -> str:
    """The value stored for a claim: the new cursor, then the claim IDs, newest first.

    Built here for both :func:`claim_email_window` and :func:`settle_email_claim`, so a
    retried write carries exactly the bytes of the original. That is what lets either of
    the two commit with the same result, and the claim ID in them is how a later call
    recognizes the claim as its own.
    """
    claims = [claim_id, *prior_claims][:MAX_RECENT_CLAIMS]
    return "\n".join([new_cursor.strip(), *claims])


def settle_email_claim(
    new_cursor: str,
    etag: str | None,
    *,
    claim_id: str,
    prior_claims: tuple[str, ...] = (),
) -> bool | None:
    """Find out how an earlier claim came out, for a caller holding its batch. Never raises.

    Call this with the arguments :func:`claim_email_window` was given, after it returned
    ``UNKNOWN``, and again until it answers. True means the claim is ours, so push the
    batch. False means it is not, because another write landed first or the key is gone,
    and a write made with *etag* can no longer commit against either, so drop the batch.
    None means nothing can be said yet: keep holding and ask again.

    A write that errored may still be in flight, since a timed-out request can commit
    after the client has given up on it. Reading the old cursor back proves nothing, so
    the conditional write itself settles it. In order:

    1. Read the cursor with its ETag. A failed read is None.
    2. A missing key is False.
    3. A value whose first line isn't a timestamp, or one that came back without an ETag,
       is None. It can be read but says nothing about the claim (a proxy answering 200
       with an error page, say), exactly as :func:`read_email_cursor` calls it UNRESOLVED.
    4. *claim_id* among the stored claim IDs is True, whether the claim is the newest or
       sits behind other instances' later ones, which carry it forward.
    5. A current ETag that differs from *etag* is False: another write landed, so ours can
       no longer commit.
    6. The same ETag means nothing has landed yet, so retry the same conditional write,
       with the same value and *etag*. If it succeeds the claim is ours, and the original
       can never commit, because its If-Match is now stale. ``ConcurrencyError`` and any
       other error are None: the original may just have landed, and the next call reads
       who won.

    The history is bounded, so a claim that did land reads as False once more than
    :data:`MAX_RECENT_CLAIMS` others have followed it. That takes this instance being cut
    off from storage while the others keep claiming. The mail is then not fetched again,
    though it stays readable through ``read_email``.
    """
    try:
        backend = get_backend()
        raw, current_etag = backend.read_text_with_etag(EMAIL_CURSOR_KEY)
    except Exception as exc:  # noqa: BLE001 — can't tell; the caller holds its batch.
        logger.warning(
            "Could not re-read the email cursor to settle a claim: %s",
            type(exc).__name__,
        )
        return None

    if raw is None:
        return False
    if not _is_timestamp(_cursor_line(raw)) or not current_etag:
        logger.warning(
            "Email cursor can't settle a claim: its value is not a timestamp or it has "
            "no ETag (holding the batch)"
        )
        return None
    if claim_id in _claim_ids(raw):
        return True
    if current_etag != etag:
        return False

    try:
        # current_etag equals etag here, and is known to be non-empty.
        backend.write_text(
            EMAIL_CURSOR_KEY,
            _claim_value(new_cursor, claim_id, prior_claims),
            if_match=current_etag,
        )
    except ConcurrencyError:
        logger.info("Retried email claim conflicted with another write; the next call settles it")
        return None
    except Exception as exc:  # noqa: BLE001 — can't tell; the caller holds its batch.
        logger.warning(
            "Retried email claim failed (%s); the claim is still unsettled",
            type(exc).__name__,
        )
        return None
    return True


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
