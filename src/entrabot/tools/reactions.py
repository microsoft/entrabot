"""Teams message reactions: parsing, validation and change detection.

Pure functions and dataclasses, with no I/O. Three callers use them:

* ``teams.read`` turns Graph's ``reactions`` array into :class:`MessageReaction` values.
* The ``add_teams_reaction`` / ``remove_teams_reaction`` tools use
  :func:`normalize_reaction_input` to decide what may be sent to Graph.
* The background poll uses :func:`observe_reactions` and :func:`merge_reaction_ledger` to
  find reactions that are new on the agent's own messages.

A reaction is external content. Its type is reduced here to an emoji, a legacy name,
``custom`` or :data:`UNRECOGNIZED_REACTION`, so it can't carry text. Its display name is free
text that whoever made a custom reaction chose; ``teams.read`` wraps it and the push keeps it
inside the envelope.

The reaction ledger is its own state. It is never mixed with the poll's seen-message set or
with the reply-watching state (Learning #27).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

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

# An emoji is at most a handful of code points; the longest joined sequences are about ten.
MAX_REACTION_CODE_POINTS = 16
MAX_DISPLAY_NAME_LENGTH = 64
MAX_USER_ID_LENGTH = 128

# How many reactions are tracked on one message, and in one chat. The poll reads the ten
# newest messages, so everything on them always fits: 10 x 25 <= 250.
MAX_TRACKED_REACTIONS_PER_MESSAGE = 25
MAX_REACTION_KEYS = 250

# The Unicode blocks emoji come from. Tag characters (U+E0020 to U+E007F) are left out on
# purpose: they are invisible and can spell ASCII text, which costs three subdivision flags.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # pictographs, emoticons, transport, flags, skin tones
    (0x2190, 0x21FF),  # arrows
    (0x2300, 0x23FF),  # miscellaneous technical (watch, alarm clock)
    (0x2460, 0x24FF),  # enclosed alphanumerics
    (0x25A0, 0x25FF),  # geometric shapes
    (0x2600, 0x27BF),  # miscellaneous symbols, dingbats
    (0x2900, 0x297F),  # supplemental arrows
    (0x2B00, 0x2BFF),  # miscellaneous symbols and arrows (star)
)
_EMOJI_SINGLES = frozenset(
    {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299}
)
_ZWJ = 0x200D
_VARIATION_SELECTOR = 0xFE0F
_KEYCAP = 0x20E3
_JOINERS = frozenset({_ZWJ, _VARIATION_SELECTOR, _KEYCAP})
_KEYCAP_BASES = frozenset("0123456789#*")


def _is_emoji_base(code_point: int) -> bool:
    if code_point in _EMOJI_SINGLES:
        return True
    return any(low <= code_point <= high for low, high in _EMOJI_RANGES)


def is_emoji(value: str) -> bool:
    """True if *value* is one Unicode emoji and nothing else.

    It accepts skin tones, joined sequences, flags and keycaps. It refuses anything with a
    letter, a digit outside a keycap, whitespace, punctuation or a tag character, so a value
    that passes can't carry text.
    """
    if not value or len(value) > MAX_REACTION_CODE_POINTS:
        return False
    if ord(value[0]) in _JOINERS:
        return False
    has_base = False
    index = 0
    while index < len(value):
        char = value[index]
        code_point = ord(char)
        if char in _KEYCAP_BASES:
            # A digit, # or * counts only as the start of a keycap: base, optional selector, cap.
            following = index + 1
            if following < len(value) and ord(value[following]) == _VARIATION_SELECTOR:
                following += 1
            if following >= len(value) or ord(value[following]) != _KEYCAP:
                return False
            has_base = True
            index = following + 1
            continue
        if _is_emoji_base(code_point):
            has_base = True
        elif code_point not in _JOINERS:
            return False
        index += 1
    return has_base


def normalize_reaction_input(value: str) -> str:
    """The emoji to send to Graph for a reaction the caller asked for.

    A legacy name (``like``, ``heart``, ``laugh``, ``surprised``, ``sad``, ``angry``) becomes
    its emoji. A Unicode emoji is returned unchanged. Anything else raises ``ValueError``, so
    arbitrary text never reaches Graph.
    """
    candidate = (value or "").strip()
    legacy = LEGACY_REACTIONS.get(candidate.lower())
    if legacy is not None:
        return legacy
    if is_emoji(candidate):
        return candidate
    names = ", ".join(LEGACY_REACTIONS)
    raise ValueError(
        "reaction must be one Unicode emoji (for example \U0001f44d) or one of these names: "
        f"{names}. Nothing was sent."
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


def _safe_timestamp(value: object) -> str:
    """*value* if it is an ISO 8601 timestamp, else an empty string."""
    if not isinstance(value, str):
        return ""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return value


@dataclass(frozen=True)
class MessageReaction:
    """One reaction on a Teams chat message.

    ``display_name`` is the reaction's name, not a person's. For a custom reaction it is text
    its creator chose, so it is untrusted.
    """

    reaction_type: str
    display_name: str
    user_id: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "reaction_type": self.reaction_type,
            "display_name": self.display_name,
            "user_id": self.user_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> MessageReaction | None:
        """Read a reaction back from the form ``teams.read`` emits, or None if it isn't one."""
        if not isinstance(raw, Mapping) or not raw.get("reaction_type"):
            return None
        return cls(
            reaction_type=_safe_reaction_type(raw.get("reaction_type")),
            display_name=str(raw.get("display_name") or ""),
            user_id=str(raw.get("user_id") or "").strip(),
            created_at=_safe_timestamp(raw.get("created_at")),
        )

    @property
    def key_type(self) -> str:
        """The reaction type as the ledger keys it.

        Every custom reaction has the type ``custom``, so the key adds a digest of the name to
        tell two apart. A digest, not the name, so the ledger holds no free text.
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
    """Read a Graph chatMessage's ``reactions`` array. Malformed entries are skipped."""
    if not isinstance(raw, list):
        return []
    parsed: list[MessageReaction] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not entry.get("reactionType"):
            continue
        parsed.append(
            MessageReaction(
                reaction_type=_safe_reaction_type(entry.get("reactionType")),
                display_name=str(entry.get("displayName") or "").strip()[:MAX_DISPLAY_NAME_LENGTH],
                user_id=_reactor_id(entry.get("user")),
                created_at=_safe_timestamp(entry.get("createdDateTime")),
            )
        )
    return parsed


@dataclass(frozen=True, order=True)
class ReactionKey:
    """What identifies one reaction for deduplication: which message, who, and which reaction."""

    message_id: str
    user_id: str
    reaction_type: str


def _message_order(message_id: str) -> tuple[int, str]:
    """Sort key that puts Teams message IDs, which are numeric strings, oldest first."""
    return (len(message_id), message_id)


def _key_order(key: ReactionKey) -> tuple[tuple[int, str], str, str]:
    return (_message_order(key.message_id), key.user_id, key.reaction_type)


def reaction_keys_to_json(keys: Iterable[ReactionKey]) -> list[list[str]]:
    """The ledger as it is stored in the chat cursor: a sorted list of triples."""
    return [
        [key.message_id, key.user_id, key.reaction_type] for key in sorted(keys, key=_key_order)
    ]


def reaction_keys_from_json(raw: object) -> set[ReactionKey] | None:
    """Read a stored ledger. None when there isn't one, which is not the same as an empty one.

    None means the reaction state was never built for this chat, so the first observation
    seeds it without pushing anything. Malformed entries are skipped.
    """
    if not isinstance(raw, list):
        return None
    keys: set[ReactionKey] = set()
    for entry in raw:
        if (
            isinstance(entry, list)
            and len(entry) == 3
            and all(isinstance(part, str) and part for part in entry)
        ):
            keys.add(ReactionKey(entry[0], entry[1], entry[2]))
    return keys


@dataclass(frozen=True)
class ReactionObservation:
    """The reactions one poll cycle saw on the agent's own messages.

    ``message_ids`` are the agent's messages that were in the read, with or without reactions.
    A ledger entry for one of them that is no longer in ``reactions`` was removed. An entry
    for any other message says nothing either way and is kept.
    """

    message_ids: frozenset[str]
    reactions: Mapping[ReactionKey, MessageReaction] = field(default_factory=dict)

    @property
    def keys(self) -> frozenset[ReactionKey]:
        return frozenset(self.reactions)


def observe_reactions(
    messages: Iterable[Mapping],
    *,
    is_own_message: Callable[[Mapping], bool],
    self_user_ids: Collection[str],
) -> ReactionObservation:
    """Collect other people's reactions on the agent's own messages.

    *is_own_message* decides which messages are the agent's; it must identify the agent by UPN
    or object ID, never by display name. *self_user_ids* are the IDs the agent reacts as, and
    reactions from them are left out. So are reactions with no user ID, which can't be keyed.

    Each message contributes at most :data:`MAX_TRACKED_REACTIONS_PER_MESSAGE` reactions, the
    earliest ones, chosen the same way whatever order Graph returned them in.
    """
    self_ids = {str(user_id).strip().lower() for user_id in self_user_ids if user_id}
    message_ids: set[str] = set()
    reactions: dict[ReactionKey, MessageReaction] = {}
    for message in messages:
        message_id = str(message.get("message_id") or "")
        if not message_id or not is_own_message(message):
            continue
        message_ids.add(message_id)
        raw_reactions = message.get("reactions")
        candidates: list[MessageReaction] = []
        for raw in raw_reactions if isinstance(raw_reactions, list) else []:
            reaction = MessageReaction.from_dict(raw)
            if reaction is None or not reaction.user_id:
                continue
            if reaction.user_id.lower() in self_ids:
                continue
            candidates.append(reaction)
        candidates.sort(key=lambda r: (r.created_at, r.user_id, r.key_type))
        for reaction in candidates[:MAX_TRACKED_REACTIONS_PER_MESSAGE]:
            reactions[ReactionKey(message_id, reaction.user_id, reaction.key_type)] = reaction
    return ReactionObservation(message_ids=frozenset(message_ids), reactions=reactions)


@dataclass(frozen=True)
class ReactionLedgerUpdate:
    """The ledger after an observation, and the reactions that were not in it before."""

    ledger: frozenset[ReactionKey]
    added: tuple[ReactionKey, ...]


def merge_reaction_ledger(
    ledger: Collection[ReactionKey], observation: ReactionObservation
) -> ReactionLedgerUpdate:
    """Apply *observation* to *ledger*.

    * A reaction that is observed and not in the ledger is ``added``.
    * A ledger entry for an observed message that is no longer there was removed, and leaves
      the ledger, so adding it again is seen again.
    * A ledger entry for a message that wasn't in the read is kept. A short or empty read
      must not make every reaction look new on the next full one.

    The result holds at most :data:`MAX_REACTION_KEYS` entries. Entries for messages outside
    the read are dropped to fit, oldest message first. What was observed is never dropped,
    because the next cycle would take it for new and push it again.
    """
    known = set(ledger)
    current = observation.keys
    added = tuple(sorted(current - known, key=_key_order))
    kept = sorted(
        (key for key in known - current if key.message_id not in observation.message_ids),
        key=_key_order,
    )
    room = max(0, MAX_REACTION_KEYS - len(current))
    retained = kept[len(kept) - room :] if room else []
    return ReactionLedgerUpdate(ledger=frozenset(current) | frozenset(retained), added=added)


__all__ = [
    "CUSTOM_REACTION",
    "LEGACY_REACTIONS",
    "MAX_REACTION_KEYS",
    "MAX_TRACKED_REACTIONS_PER_MESSAGE",
    "UNRECOGNIZED_REACTION",
    "MessageReaction",
    "ReactionKey",
    "ReactionLedgerUpdate",
    "ReactionObservation",
    "is_emoji",
    "merge_reaction_ledger",
    "normalize_reaction_input",
    "observe_reactions",
    "parse_reactions",
    "reaction_keys_from_json",
    "reaction_keys_to_json",
]
