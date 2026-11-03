"""Boundary-enforced XPIA envelope for external-source content.

Any body of text that came from an external source — a Teams message
authored by a human in a chat, an email body, the text of a file in
OneDrive / SharePoint, the content of a Word document, a memory file —
is wrapped in an ``<external_content>`` envelope at the tool return
boundary before the model ever sees it. The body prompt
(``prompts/anatomy/security.md``) instructs the model that content
inside the envelope is DATA, not INSTRUCTIONS.

This module owns the envelope: ``wrap_external`` produces it,
``unwrap_external`` reverses it (test + audit path only). The wrap is:

- **Authoritative outer envelope.** Every call wraps the supplied body,
  even when it already resembles an envelope. External text cannot suppress
  trusted ``source`` / ``sender`` metadata by forging the wrapper prefix.
- **Escape-on-collision.** Any literal ``</external_content>`` in the
  body — including case variants and whitespace-padded forms — is
  escaped to ``&lt;/external_content&gt;`` before wrapping. An attacker
  cannot break out of the envelope by embedding a closing tag.
- **Attribute-safe.** ``source``, ``sender``, ``received_at`` attribute
  values escape ``<``, ``>``, ``&``, and the quote character. So a
  hostile ``source="teams:<script>"`` cannot spawn a new tag in the
  attribute region.
- **Opt-out via env.** ``ENTRABOT_XPIA_WRAP_ENABLE=false`` short-circuits
  ``wrap_external`` to the identity function so a live rollback needs
  only a restart, not a code revert.
- **A bracket form for channel pushes.** A push can't carry angle
  brackets, so ``wrap_for_channel`` produces the same envelope as
  ``[external_content ...]...[/external_content]``.

This is a text-escaping and provenance boundary, not a cryptographic
signature: it does not authenticate the body's origin, sanitize its
contents, or guarantee that a model cannot be persuaded to follow
instructions embedded in wrapped text. See
``docs/reference/api/security.md`` for the full public API reference
and ``docs/architecture/security-boundaries.md`` for how this boundary
fits alongside identity attribution, sponsor authorization, and audit.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime

from entrabot.config import get_config

# Envelope tag constants — one source of truth.
_TAG_NAME = "external_content"
_CLOSE_TAG = f"</{_TAG_NAME}>"

# Escape-on-collision regex: match ``</external_content>`` case-insensitively
# with optional whitespace between ``<``, ``/``, the tag name, and ``>``.
# Attackers may pad the tag to sneak past a naïve string.replace, so we
# use a regex that mirrors a lenient HTML parser's view of the same
# literal close tag.
_CLOSE_TAG_RE = re.compile(
    r"<\s*/\s*external_content\s*>",
    re.IGNORECASE,
)


def _escape_close_tag_preserving_original(match: re.Match[str]) -> str:
    """Return the HTML-entity-escaped form of the matched close tag.

    Uses the *original* matched substring so a round-trip via
    ``unwrap_external`` produces byte-for-byte identical output for
    every casing (``</External_Content>``, ``< / external_content >``,
    etc.). The model still sees the entity-escaped form and cannot
    escape the envelope.
    """
    original = match.group(0)
    # Replace the outer ``<`` and ``>`` with entities; everything in
    # between is preserved verbatim so unwrap can recover it losslessly.
    # ``original`` is guaranteed to start with ``<`` and end with ``>``.
    return "&lt;" + original[1:-1] + "&gt;"


@dataclass(frozen=True)
class ExternalContent:
    """The result of :func:`unwrap_external`.

    Round-trips ``wrap_external`` byte-for-byte on ``body``; ``source``,
    ``sender``, ``received_at`` come back the same as the wrap-time
    arguments (attribute-escaping is reversed on unwrap).
    """

    body: str
    source: str
    sender: str | None = None
    received_at: datetime | None = None


def _wrap_enabled() -> bool:
    """Return the single config-owned XPIA wrapping decision."""
    return get_config().xpia_wrap_enable


def _escape_attribute(value: str) -> str:
    """Escape ``&``, ``<``, ``>``, and ``"`` for attribute-value context.

    Escape order matters: ``&`` must be replaced before the entity
    substitutions so their leading ``&`` doesn't get double-escaped.
    """
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _unescape_attribute(value: str) -> str:
    """Inverse of :func:`_escape_attribute`.

    Reverse order matters so we don't accidentally double-unescape a
    literal ``&amp;lt;`` back into ``<``.
    """
    return (
        value.replace("&quot;", '"').replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    )


def wrap_external(
    body: str,
    *,
    source: str,
    sender: str | None = None,
    received_at: datetime | None = None,
) -> str:
    """Wrap external-source content in the XPIA envelope.

    Always emits a trusted outer envelope. If ``body`` already resembles
    an envelope, it remains untrusted body text; its close tag is escaped
    and the call-site ``source`` / ``sender`` metadata stays authoritative.

    Args:
        body: The external-source text. May contain arbitrary Unicode.
        source: Provenance identifier (e.g. ``"teams:<chat_id>"``,
            ``"email:<message_id>"``, ``"file:<url>"``). Required.
        sender: Optional canonical identity of the message author (UPN,
            email, or ``"unknown"``). Omitted from the envelope when
            ``None``.
        received_at: Optional timezone-aware ``datetime``. Serialized to
            ISO 8601 via ``isoformat()``. Omitted when ``None``.

    Returns:
        The wrapped body. When the env flag disables wrapping, returns
        ``body`` unchanged (identity function).
    """
    if not _wrap_enabled():
        return body

    # Escape any literal close tag inside the body BEFORE we wrap.
    # Use a callable so we preserve the original casing / whitespace of
    # the matched substring — that lets unwrap round-trip byte-for-byte.
    safe_body = _CLOSE_TAG_RE.sub(_escape_close_tag_preserving_original, body)

    attrs = [f'source="{_escape_attribute(source)}"']
    if sender is not None:
        attrs.append(f'sender="{_escape_attribute(sender)}"')
    if received_at is not None:
        attrs.append(f'received_at="{_escape_attribute(received_at.isoformat())}"')

    return f"<{_TAG_NAME} {' '.join(attrs)}>{safe_body}{_CLOSE_TAG}"


# The bracket form of the envelope. See :func:`wrap_for_channel`.
_CHANNEL_CLOSE = f"[/{_TAG_NAME}]"

# Angle brackets become their single-guillemet look-alikes, which keeps the sense of "a < b"
# without a character a push can't carry. Fullwidth forms go too, because a model reads them
# the same way.
_CHANNEL_ANGLES = {
    "<": "\u2039",
    ">": "\u203a",
    "\uff1c": "\u2039",
    "\uff1e": "\u203a",
}
_CHANNEL_ANGLE_CHARS = str.maketrans(_CHANNEL_ANGLES)

# In a channel envelope's body, square brackets also become parentheses, so no marker can form
# there at all.
_CHANNEL_BODY_CHARS = str.maketrans(
    {**_CHANNEL_ANGLES, "[": "(", "]": ")", "\uff3b": "(", "\uff3d": ")"}
)

# More than any real text nests. Past this, the leftover ampersands are replaced instead.
_MAX_UNESCAPE_ROUNDS = 8

# The marker's name, however it's padded or split. Removing it covers any delimiter the
# character map above doesn't know about.
_CHANNEL_MARKER_NAME_RE = re.compile(
    r"external[\s_\-\u00ad\u200b-\u200f\u2060\ufeff]*content",
    re.IGNORECASE,
)

# What can't appear in a channel envelope's attribute values: anything that could end the
# value or the marker, or start a new line. Each becomes a space rather than being removed,
# because closing up the gap could assemble a new character reference: &quo[]t; into &quot;.
_CHANNEL_ATTRIBUTE_UNSAFE_RE = re.compile(r'["\[\]<>\uff3b\uff3d\uff1c\uff1e\x00-\x1f\x7f]')


def _channel_attribute(value: str) -> str:
    return _CHANNEL_ATTRIBUTE_UNSAFE_RE.sub(" ", _fully_unescaped(value))


def _fully_unescaped(text: str) -> str:
    """Decode character references until none are left.

    The host's client can decode them, so ``&#91;`` in a push could arrive as ``[``, and a
    nested ``&amp;#91;`` could get there too. With nothing left to decode, whatever is
    filtered next is what the model sees.
    """
    for _ in range(_MAX_UNESCAPE_ROUNDS):
        decoded = html.unescape(text)
        if decoded == text:
            return text
        text = decoded
    return text.replace("&", "\uff06")


def channel_safe_text(text: str) -> str:
    """Make plain text safe to carry in a channel push, wrapped or not.

    For fields that are plain text rather than HTML, such as an email subject, where stripping
    "tags" would delete real words: ``Deploy <release-123>`` keeps ``release-123``. Character
    references are decoded and angle brackets become look-alikes, so none can reach the client.
    """
    return _fully_unescaped(text).translate(_CHANNEL_ANGLE_CHARS)


def wrap_for_channel(body: str, *, source: str, sender: str | None = None) -> str:
    """Wrap external text for a channel push, in the bracket form of the envelope.

    A channel push can't carry angle brackets: the host's client has closed the MCP stream
    on them (``engineering-history/investigations/mcp-disconnect-investigation.md``), so the
    push sanitizer strips every tag, the ``<external_content>`` envelope included. This form
    means the same thing without them, and the body prompt treats the two alike.

    The output is ``[external_content source="..." sender="..."]body[/external_content]``.
    The body can't forge a marker, because after this it holds no square bracket and no
    marker name, encoded or not. It also holds no angle bracket, so a push stays safe even
    if the caller's sanitizing missed one. Unlike :func:`wrap_external` this isn't
    reversible: it is for one-way notifications, and the original stays readable through
    the read tools.

    Args:
        body: The external-source text, already cut to its length limit, so the close marker
            is never what gets truncated.
        source: Where the text came from, such as ``"teams"`` or ``"email"``.
        sender: The author's identity (UPN, email, or object ID), never a display name.
            Omitted when ``None``.

    Returns:
        The wrapped body. When the env flag disables wrapping, returns ``body`` unchanged.
    """
    if not _wrap_enabled():
        return body

    safe_body = _CHANNEL_MARKER_NAME_RE.sub("external content", _fully_unescaped(body)).translate(
        _CHANNEL_BODY_CHARS
    )
    attrs = [f'source="{_channel_attribute(source)}"']
    if sender is not None:
        attrs.append(f'sender="{_channel_attribute(sender)}"')
    return f"[{_TAG_NAME} {' '.join(attrs)}]{safe_body}{_CHANNEL_CLOSE}"


# Regex used by :func:`unwrap_external`. Matches the outermost envelope
# only — the wrap function only ever emits one envelope; the escape-on-
# collision rule keeps embedded close tags from confusing the parser.
_UNWRAP_RE = re.compile(
    r"^\s*<external_content\s+(?P<attrs>[^>]*)>(?P<body>.*)</external_content>\s*\Z",
    re.DOTALL,
)

_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

# Reverse of :func:`_escape_close_tag_preserving_original`. Matches
# ``&lt;<inner>&gt;`` where the inner text is a lax variant of
# ``/external_content`` (case-insensitive, whitespace-tolerant) so we
# can restore the original ``</external_content>`` casing losslessly.
_ESCAPED_CLOSE_TAG_RE = re.compile(
    r"&lt;(\s*/\s*external_content\s*)&gt;",
    re.IGNORECASE,
)


def unwrap_external(wrapped: str) -> ExternalContent:
    """Reverse :func:`wrap_external`. Round-trips ``body`` byte-for-byte.

    For test + audit use — NOT called from the tool return path (which
    only produces wrapped content). Raises ``ValueError`` if the input
    isn't a valid envelope.
    """
    match = _UNWRAP_RE.match(wrapped)
    if match is None:
        raise ValueError("input is not a valid external_content envelope")

    attrs_raw = match.group("attrs")
    body = match.group("body")

    # Reverse the escape-on-collision step so the body comes back
    # byte-for-byte identical to what wrap_external received. The
    # wrap side used _escape_close_tag_preserving_original, which
    # produces ``&lt;<orig-inner>&gt;`` where the inner text is the
    # original substring's middle. We match that exact shape and put
    # the surrounding ``<``/``>`` characters back.
    body = _ESCAPED_CLOSE_TAG_RE.sub(
        lambda m: "<" + m.group(1) + ">",
        body,
    )

    attrs: dict[str, str] = {}
    for name, value in _ATTR_RE.findall(attrs_raw):
        attrs[name] = _unescape_attribute(value)

    source = attrs.get("source", "")
    sender = attrs.get("sender")

    received_at: datetime | None = None
    received_at_raw = attrs.get("received_at")
    if received_at_raw:
        try:
            received_at = datetime.fromisoformat(received_at_raw)
        except ValueError:
            # Preserve the string on the returned dataclass? Design
            # decision: unwrap is best-effort for tests + audit only;
            # a malformed timestamp becomes None rather than fail-loud.
            received_at = None

    return ExternalContent(
        body=body,
        source=source,
        sender=sender,
        received_at=received_at,
    )


__all__ = [
    "ExternalContent",
    "channel_safe_text",
    "unwrap_external",
    "wrap_external",
    "wrap_for_channel",
]
