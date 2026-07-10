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

- **Idempotent.** ``wrap(wrap(x)) == wrap(x)``. A caller passing
  already-wrapped input receives it unchanged (no double-envelope), so
  wire-through code that re-wraps by accident does no harm.
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

See ``docs/architecture/PLAN-xpia-content-wrapping.md`` (landing in
PR #99) for the full design + rollout notes, and Learning #70 in
``docs/runbooks/hard-won-learnings.md`` for the motivation.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("entrabot.security.xpia")

# Envelope tag constants — one source of truth.
_TAG_NAME = "external_content"
_OPEN_PREFIX = f"<{_TAG_NAME} "
_OPEN_NO_ATTRS = f"<{_TAG_NAME}"
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

# Sentinel escaped forms:
#   - _CLOSE_TAG_ESCAPED: the canonical hit; the unwrap path replaces
#     this back to the lower-case literal close tag so a round-trip
#     from ``wrap_external`` produces byte-identical output on that
#     specific casing. Case variants (``</EXTERNAL_CONTENT>`` etc.)
#     round-trip through a per-match encoded form (see below).
_CLOSE_TAG_ESCAPED = "&lt;/external_content&gt;"


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
    """Return True when the XPIA wrap is active.

    Default is enabled — the flag is a rollback path. Any truthy string
    (``true`` / ``1`` / ``yes``, case-insensitive) means enabled; any
    explicitly falsy string (``false`` / ``0`` / ``no``) disables. An
    absent env var defaults to enabled.
    """
    raw = os.environ.get("ENTRABOT_XPIA_WRAP_ENABLE")
    if raw is None:
        return True
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


def _escape_attribute(value: str) -> str:
    """Escape ``&``, ``<``, ``>``, and ``"`` for attribute-value context.

    Escape order matters: ``&`` must be replaced before the entity
    substitutions so their leading ``&`` doesn't get double-escaped.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _unescape_attribute(value: str) -> str:
    """Inverse of :func:`_escape_attribute`.

    Reverse order matters so we don't accidentally double-unescape a
    literal ``&amp;lt;`` back into ``<``.
    """
    return (
        value.replace("&quot;", '"')
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&amp;", "&")
    )


def _already_wrapped(body: str) -> bool:
    """Cheap check: is ``body`` already an ``external_content`` envelope?

    We accept either an attribute-bearing prefix (the normal case) or the
    bare ``<external_content>`` (defensive: an attacker who somehow gets
    the body pre-shaped this way still gets treated as wrapped so we
    don't double-envelope, and the outer wrap is a no-op that leaves the
    attacker's bogus envelope in place — which the model will refuse to
    act on per the body prompt).
    """
    if not body:
        return False
    lstripped = body.lstrip()
    # Prefer a strict prefix check so we don't false-match a body that
    # merely mentions ``<external_content>`` somewhere in its middle.
    return (
        lstripped.startswith(_OPEN_PREFIX)
        or lstripped.startswith(_OPEN_NO_ATTRS + ">")
    )


def wrap_external(
    body: str,
    *,
    source: str,
    sender: str | None = None,
    received_at: datetime | None = None,
) -> str:
    """Wrap external-source content in the XPIA envelope.

    Idempotent — if ``body`` already begins with the envelope's open
    tag, we return it unchanged (with a debug log). Escape-on-collision
    handles literal ``</external_content>`` inside the body.

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

    if _already_wrapped(body):
        # Second-wrap suppressed — this is the idempotency contract.
        logger.debug(
            "wrap_external: input already wrapped, skipping (source=%s)", source
        )
        return body

    # Escape any literal close tag inside the body BEFORE we wrap.
    # Use a callable so we preserve the original casing / whitespace of
    # the matched substring — that lets unwrap round-trip byte-for-byte.
    safe_body = _CLOSE_TAG_RE.sub(
        _escape_close_tag_preserving_original, body
    )

    attrs = [f'source="{_escape_attribute(source)}"']
    if sender is not None:
        attrs.append(f'sender="{_escape_attribute(sender)}"')
    if received_at is not None:
        attrs.append(f'received_at="{_escape_attribute(received_at.isoformat())}"')

    return f"<{_TAG_NAME} {' '.join(attrs)}>{safe_body}{_CLOSE_TAG}"


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
    "unwrap_external",
    "wrap_external",
]
