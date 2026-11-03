"""Limits on the text a file read returns to the model."""

from __future__ import annotations

import os

DEFAULT_MAX_TEXT_BYTES = 200_000


def max_text_bytes() -> int:
    """Return ``ENTRABOT_FILES_MAX_TEXT_BYTES``, or the default if unset or unparseable."""
    raw = os.environ.get("ENTRABOT_FILES_MAX_TEXT_BYTES")
    try:
        return int(raw) if raw else DEFAULT_MAX_TEXT_BYTES
    except ValueError:
        return DEFAULT_MAX_TEXT_BYTES


def truncate_utf8(text: str, *, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` so its UTF-8 encoding is at most ``max_bytes``.

    Returns the text and whether it was cut. A multi-byte character that would
    straddle the limit is dropped rather than split.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True
