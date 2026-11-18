"""Teams message reactions: parsing, validation and change detection.

Pure functions and dataclasses, with no I/O. Three callers use them:

* ``teams.read`` turns Graph's ``reactions`` array into :class:`MessageReaction` values.
* The ``add_teams_reaction`` / ``remove_teams_reaction`` tools use
  :func:`normalize_reaction_input` to decide what may be sent to Graph.
* The background poll uses :func:`observe_reactions` and :func:`advance_reaction_ledger` to
  find reactions that are new on the agent's own messages.

A reaction is external content. Its type is reduced here to exactly one emoji, a legacy name,
``custom`` or :data:`UNRECOGNIZED_REACTION`, so it can't carry text. Its display name is free
text that whoever made a custom reaction chose; ``teams.read`` wraps it and the push keeps it
inside the envelope.

The reaction ledger is its own state. It is never mixed with the poll's seen-message set or
with the reply-watching state (Learning #27). Three rules shape it:

* **It only grows by observation.** Several instances can poll one chat, and one of them may
  be working from an older read than another. So nothing is ever removed because a reader
  didn't see it. A reaction that is removed and added again is still noticed, because the
  key includes the reaction's creation time and a re-add has a new one.
* **Old reactions are not news.** A reaction older than :data:`REACTION_MAX_AGE_SECONDS`
  is never announced, whatever the ledger says. That covers a ledger that went stale while
  notifications were switched off or the server was down. Entries are dropped once they are
  older than :data:`REACTION_RETENTION_SECONDS`, which is safe for every reader because
  nothing that old can be announced by anyone.
* **Past the size limit it stays quiet.** If a chat collects more than
  :data:`MAX_REACTION_KEYS` recent reactions, the oldest entries are dropped and the ledger
  keeps a floor: a reaction created at or before the floor is never announced.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# What the six reaction names Teams used before Unicode reactions mean. Graph still returns
# and accepts the names; the tools send the emoji.
LEGACY_REACTIONS: dict[str, str] = {
    "like": "\U0001f44d",
    "heart": "❤️",
    "laugh": "\U0001f606",
    "surprised": "\U0001f62e",
    "sad": "\U0001f641",
    "angry": "\U0001f620",
}

CUSTOM_REACTION = "custom"
UNRECOGNIZED_REACTION = "unrecognized"

# One emoji is at most a handful of code points; the longest joined sequences are about ten.
MAX_REACTION_CODE_POINTS = 16
# A family or a couple is four emoji joined together. Nothing real is longer.
MAX_JOINED_EMOJI = 4
MAX_DISPLAY_NAME_LENGTH = 64
MAX_USER_ID_LENGTH = 128

# A reaction older than this is never announced.
REACTION_MAX_AGE_SECONDS = 60 * 60
# How long a ledger entry is kept. Longer than the maximum age, so an entry is only dropped
# once its reaction is too old for any instance to announce.
REACTION_RETENTION_SECONDS = 2 * 60 * 60
# Most entries one chat's ledger holds. See the module docstring for what happens past it.
MAX_REACTION_KEYS = 500

# The cursor fields the ledger is stored in. Owned here, written by ``chat_cursors``.
REACTION_KEYS_FIELD = "reaction_keys_tail"
REACTION_FLOOR_FIELD = "reaction_floor"

# Code points that are pictures. Left out on purpose, because they read as letters, digits
# or punctuation and a reaction type must not be able to carry text: enclosed alphanumerics
# and their supplement (which includes the regional indicators flags are made of), keycaps,
# arrows, the copyright, registered, trade mark and information signs, circled ideographs,
# dingbat digits and bracket ornaments, playing cards, and tag characters. That costs
# country flags, keycap digits and arrow emoji.
_PICTOGRAPH_RANGES: tuple[tuple[int, int], ...] = (
    (0x231A, 0x231B),  # watch, hourglass
    (0x23E9, 0x23F3),  # media buttons, alarm clock, hourglass
    (0x23F8, 0x23FA),  # pause, stop, record
    (0x2600, 0x26FF),  # miscellaneous symbols
    (0x2700, 0x275A),  # dingbats, up to the quotation ornaments
    (0x2761, 0x2767),  # hearts and florets
    (0x2B1B, 0x2B1C),  # large squares
    (0x1F300, 0x1F3FA),  # miscellaneous symbols and pictographs, up to the skin tones
    (0x1F400, 0x1F518),  # ... up to the word arrows (BACK, END, ON!, SOON, TOP) and keycaps
    (0x1F525, 0x1F5FF),
    (0x1F600, 0x1F64F),  # emoticons
    (0x1F680, 0x1F6FF),  # transport and map symbols
    (0x1F7E0, 0x1F7EB),  # coloured circles and squares
    (0x1F900, 0x1F9FF),  # supplemental symbols and pictographs
    (0x1FA70, 0x1FAFF),  # symbols and pictographs extended-A
)
_PICTOGRAPH_SINGLES = frozenset({0x2B50, 0x2B55})  # star, hollow circle
_ZWJ = 0x200D
_VARIATION_SELECTOR = 0xFE0F
_SKIN_TONES = range(0x1F3FB, 0x1F400)


def _is_pictograph(code_point: int) -> bool:
    if code_point in _PICTOGRAPH_SINGLES:
        return True
    return any(low <= code_point <= high for low, high in _PICTOGRAPH_RANGES)


def _after_emoji_element(code_points: list[int], index: int) -> int | None:
    """The index after one pictograph and its optional selector and skin tone, or None."""
    if index >= len(code_points) or not _is_pictograph(code_points[index]):
        return None
    index += 1
    seen_selector = seen_tone = False
    while index < len(code_points):
        code_point = code_points[index]
        if code_point == _VARIATION_SELECTOR and not seen_selector:
            seen_selector = True
        elif code_point in _SKIN_TONES and not seen_tone:
            seen_tone = True
        else:
            break
        index += 1
    return index


def is_emoji(value: str) -> bool:
    """True if *value* is exactly one emoji and nothing else.

    One emoji is a pictograph with an optional variation selector and skin tone, or up to
    :data:`MAX_JOINED_EMOJI` of those joined by zero-width joiners (a family, a profession).
    Two emoji side by side are refused, and so is anything that reads as a letter or a digit;
    see ``_PICTOGRAPH_RANGES`` for what that leaves out. A value that passes can't carry text.
    """
    code_points = [ord(char) for char in value or ""]
    if not code_points or len(code_points) > MAX_REACTION_CODE_POINTS:
        return False
    index = _after_emoji_element(code_points, 0)
    joined = 1
    while index is not None and index < len(code_points):
        if code_points[index] != _ZWJ or joined >= MAX_JOINED_EMOJI:
            return False
        index = _after_emoji_element(code_points, index + 1)
        joined += 1
    return index is not None


def normalize_reaction_input(value: str) -> str:
    """The emoji to send to Graph for a reaction the caller asked for.

    A legacy name (``like``, ``heart``, ``laugh``, ``surprised``, ``sad``, ``angry``) becomes
    its emoji. One Unicode emoji is returned unchanged. Anything else raises ``ValueError``,
    so arbitrary text never reaches Graph.
    """
    candidate = (value or "").strip()
    legacy = LEGACY_REACTIONS.get(candidate.lower())
    if legacy is not None:
        return legacy
    if is_emoji(candidate):
        return candidate
    names = ", ".join(LEGACY_REACTIONS)
    raise ValueError(
        "reaction must be exactly one Unicode emoji (for example \U0001f44d) or one of these "
        f"names: {names}. Flags, keycap digits, arrows and letter-like symbols are not "
        "accepted. Nothing was sent."
    )


def _safe_reaction_type(value: object) -> str:
    """Reduce a reaction type from Graph to something that can't carry text."""
    candidate = str(value or "").strip()
    lowered = candidate.lower()
    if lowered in LEGACY_REACTIONS or lowered == CUSTOM_REACTION:
        return lowered
    if is_emoji(candidate):
        return candidate
    return UNRECOGNIZED_REACTION


def parse_timestamp(value: object) -> datetime | None:
    """*value* as an aware UTC datetime, or None if it isn't an ISO 8601 timestamp."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def canonical_timestamp(value: object) -> str:
    """One spelling for one instant, to the millisecond, or ``""`` if *value* isn't a time.

    Graph may write the same instant with a different number of fractional digits from one
    read to the next. A ledger key has to be the same both times.
    """
    parsed = value if isinstance(value, datetime) else parse_timestamp(value)
    return parsed.isoformat(timespec="milliseconds") if parsed is not None else ""


@dataclass(frozen=True)
class MessageReaction:
    """One reaction on a Teams chat message, as Graph returned it.

    ``display_name`` is the reaction's name, not a person's, and is the raw text. For a custom
    reaction it is text its creator chose, so it is untrusted: wrap it before a model sees it.
    """

    reaction_type: str
    display_name: str
    user_id: str
    created_at: str

    @property
    def key_type(self) -> str:
        """The reaction type as the ledger keys it.

        Every custom reaction has the type ``custom``, so the key adds a digest of the name to
        tell two apart. A digest, not the name, so the ledger holds no free text; and of the
        raw name, so the key doesn't depend on how the name is wrapped for the model.
        """
        if self.reaction_type != CUSTOM_REACTION:
            return self.reaction_type
        digest = hashlib.sha256(self.display_name.encode("utf-8")).hexdigest()[:12]
        return f"{CUSTOM_REACTION}:{digest}"


def _reactor_id(identity_set: object) -> str:
    """The ID of whoever reacted: the user, else the application or device."""
    if not isinstance(identity_set, Mapping):
        return ""
    for kind in ("user", "application", "device"):
        identity = identity_set.get(kind)
        if isinstance(identity, Mapping) and identity.get("id"):
            return str(identity["id"]).strip()[:MAX_USER_ID_LENGTH]
    return ""


def parse_reactions(raw: object) -> list[MessageReaction]:
    """Read a Graph chatMessage's ``reactions`` array, all of it. Malformed entries are skipped."""
    if not isinstance(raw, list):
        return []
    parsed: list[MessageReaction] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not entry.get("reactionType"):
            continue
        created = entry.get("createdDateTime")
        parsed.append(
            MessageReaction(
                reaction_type=_safe_reaction_type(entry.get("reactionType")),
                display_name=str(entry.get("displayName") or "").strip()[:MAX_DISPLAY_NAME_LENGTH],
                user_id=_reactor_id(entry.get("user")),
                created_at=str(created) if parse_timestamp(created) is not None else "",
            )
        )
    return parsed


@dataclass(frozen=True, order=True)
class ReactionKey:
    """What identifies one reaction for deduplication.

    Which message, who, which reaction, and when it was made. ``created_at`` is in the
    :func:`canonical_timestamp` form. It is part of the key so that a reaction someone
    removes and adds again is a new key, without the ledger ever having to forget the old one.
    """

    message_id: str
    user_id: str
    reaction_type: str
    created_at: str


def _key_order(key: ReactionKey) -> tuple[str, int, str, str, str]:
    """Oldest reaction first. The canonical timestamps sort as text."""
    return (key.created_at, len(key.message_id), key.message_id, key.user_id, key.reaction_type)


def _key_from_json(entry: object) -> ReactionKey | None:
    if not (
        isinstance(entry, list)
        and len(entry) == 4
        and all(isinstance(part, str) and part for part in entry)
    ):
        return None
    created_at = canonical_timestamp(entry[3])
    return ReactionKey(entry[0], entry[1], entry[2], created_at) if created_at else None


@dataclass(frozen=True)
class ReactionLedger:
    """The reactions on the agent's own messages that have already been noticed in one chat.

    ``floor`` is empty until the ledger has had to drop entries to stay within
    :data:`MAX_REACTION_KEYS`. From then on a reaction created at or before it is never
    announced, so what was dropped can't come back as new.
    """

    keys: frozenset[ReactionKey] = frozenset()
    floor: str = ""

    def to_cursor_fields(self) -> dict[str, object]:
        """The ledger as it is stored in the chat cursor."""
        fields: dict[str, object] = {
            REACTION_KEYS_FIELD: [
                [key.message_id, key.user_id, key.reaction_type, key.created_at]
                for key in sorted(self.keys, key=_key_order)
            ]
        }
        if self.floor:
            fields[REACTION_FLOOR_FIELD] = self.floor
        return fields

    @classmethod
    def from_cursor(cls, cursor: Mapping | None) -> ReactionLedger | None:
        """Read the ledger out of a chat cursor. None when there isn't a usable one.

        None is not the same as empty. It means the reaction state was never built for this
        chat, so the first observation seeds it without announcing anything. A ledger with
        an entry that can't be read (one written before the timestamp was part of the key,
        say) also counts as not built: using the readable half would announce the rest as new.
        """
        raw = cursor.get(REACTION_KEYS_FIELD) if isinstance(cursor, Mapping) else None
        if not isinstance(raw, list):
            return None
        keys = [_key_from_json(entry) for entry in raw]
        if any(key is None for key in keys):
            return None
        return cls(
            keys=frozenset(key for key in keys if key is not None),
            floor=canonical_timestamp(cursor.get(REACTION_FLOOR_FIELD)),
        )

    def joined_with(self, other: ReactionLedger | None) -> ReactionLedger:
        """Everything either ledger knows."""
        if other is None:
            return self
        return ReactionLedger(keys=self.keys | other.keys, floor=max(self.floor, other.floor))

    def without(self, keys: Iterable[ReactionKey]) -> ReactionLedger:
        """This ledger with exactly *keys* taken out, for a claim whose push failed."""
        return ReactionLedger(keys=self.keys - frozenset(keys), floor=self.floor)

    def trimmed(self, *, now: datetime) -> ReactionLedger:
        """Drop what is too old to matter, then what doesn't fit.

        An entry older than :data:`REACTION_RETENTION_SECONDS` goes: its reaction is past the
        maximum age for every reader. If more than :data:`MAX_REACTION_KEYS` remain, the
        oldest go and the floor moves up to the newest of them.
        """
        cutoff = canonical_timestamp(now - timedelta(seconds=REACTION_RETENTION_SECONDS))
        kept = sorted((key for key in self.keys if key.created_at >= cutoff), key=_key_order)
        floor = self.floor
        overflow = len(kept) - MAX_REACTION_KEYS
        if overflow > 0:
            floor = max(floor, kept[overflow - 1].created_at)
            kept = kept[overflow:]
        return ReactionLedger(keys=frozenset(kept), floor=floor)


@dataclass(frozen=True)
class ReactionObservation:
    """The reactions one poll cycle saw that could be announced.

    Other people's reactions on the agent's own messages, recent enough to be news. It says
    nothing about reactions that are not in it: a reader can be behind, so absence is never
    taken to mean a reaction was removed.
    """

    reactions: Mapping[ReactionKey, MessageReaction] = field(default_factory=dict)

    @property
    def keys(self) -> frozenset[ReactionKey]:
        return frozenset(self.reactions)


def observe_reactions(
    messages: Iterable[Mapping],
    reactions_by_message: Mapping[str, Collection[MessageReaction]],
    *,
    is_own_message: Callable[[Mapping], bool],
    self_user_ids: Collection[str],
    now: datetime,
) -> ReactionObservation:
    """Collect other people's recent reactions on the agent's own messages.

    *reactions_by_message* holds every reaction on each message as ``parse_reactions`` read
    it, keyed by message ID. Keys are built from that, never from what a model is shown.
    *is_own_message* decides which messages are the agent's; it must identify the agent by
    UPN or object ID, never by display name. *self_user_ids* are the IDs the agent reacts as.

    Left out: the agent's own reactions; a reaction with no user ID, which can't be keyed; a
    reaction older than :data:`REACTION_MAX_AGE_SECONDS`; and a reaction whose timestamp is
    missing or unreadable, because it can't be shown to be recent.
    """
    self_ids = {str(user_id).strip().lower() for user_id in self_user_ids if user_id}
    oldest = now - timedelta(seconds=REACTION_MAX_AGE_SECONDS)
    reactions: dict[ReactionKey, MessageReaction] = {}
    for message in messages:
        message_id = str(message.get("message_id") or "")
        if not message_id or not is_own_message(message):
            continue
        for reaction in reactions_by_message.get(message_id) or ():
            if not reaction.user_id or reaction.user_id.lower() in self_ids:
                continue
            created = parse_timestamp(reaction.created_at)
            if created is None or created < oldest:
                continue
            key = ReactionKey(
                message_id, reaction.user_id, reaction.key_type, canonical_timestamp(created)
            )
            reactions[key] = reaction
    return ReactionObservation(reactions=reactions)


def unclaimed_reactions(
    ledger: ReactionLedger, observation: ReactionObservation
) -> tuple[ReactionKey, ...]:
    """The observed reactions *ledger* has not noticed yet, oldest first.

    A reaction at or before the ledger's floor is never one of them.
    """
    return tuple(
        sorted(
            (
                key
                for key in observation.keys
                if key not in ledger.keys and not (ledger.floor and key.created_at <= ledger.floor)
            ),
            key=_key_order,
        )
    )


@dataclass(frozen=True)
class ReactionLedgerUpdate:
    """The ledger after an observation, and the reactions that were not in it before."""

    ledger: ReactionLedger
    added: tuple[ReactionKey, ...]


def advance_reaction_ledger(
    ledger: ReactionLedger | None, observation: ReactionObservation, *, now: datetime
) -> ReactionLedgerUpdate:
    """Apply *observation* to *ledger*. The ledger only gains entries from it.

    With no ledger yet (None), the observation becomes the ledger and nothing is ``added``:
    reactions that were already there when the state is first built are not news. Otherwise
    ``added`` is :func:`unclaimed_reactions`, and the ledger gains them. Either way the
    result is :meth:`ReactionLedger.trimmed`.
    """
    if ledger is None:
        return ReactionLedgerUpdate(
            ledger=ReactionLedger(keys=observation.keys).trimmed(now=now), added=()
        )
    added = unclaimed_reactions(ledger, observation)
    grown = ReactionLedger(keys=ledger.keys | frozenset(added), floor=ledger.floor)
    return ReactionLedgerUpdate(ledger=grown.trimmed(now=now), added=added)


__all__ = [
    "CUSTOM_REACTION",
    "LEGACY_REACTIONS",
    "MAX_REACTION_KEYS",
    "REACTION_FLOOR_FIELD",
    "REACTION_KEYS_FIELD",
    "REACTION_MAX_AGE_SECONDS",
    "REACTION_RETENTION_SECONDS",
    "UNRECOGNIZED_REACTION",
    "MessageReaction",
    "ReactionKey",
    "ReactionLedger",
    "ReactionLedgerUpdate",
    "ReactionObservation",
    "advance_reaction_ledger",
    "canonical_timestamp",
    "is_emoji",
    "normalize_reaction_input",
    "observe_reactions",
    "parse_reactions",
    "parse_timestamp",
    "unclaimed_reactions",
]
