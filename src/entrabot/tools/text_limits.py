"""Limits on the text a file read returns to the model."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("entrabot.tools.text_limits")

DEFAULT_MAX_TEXT_BYTES = 200_000


def max_text_bytes() -> int:
    """Return the text cap. A cap that isn't a positive integer can't be meant."""
    raw = os.environ.get("ENTRABOT_FILES_MAX_TEXT_BYTES")
    if not raw:
        return DEFAULT_MAX_TEXT_BYTES
    try:
        cap = int(raw)
    except ValueError:
        cap = 0
    if cap < 1:
        logger.warning(
            "ENTRABOT_FILES_MAX_TEXT_BYTES must be a positive integer; using %d",
            DEFAULT_MAX_TEXT_BYTES,
        )
        return DEFAULT_MAX_TEXT_BYTES
    return cap


def truncate_utf8(text: str, *, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` so its UTF-8 encoding is at most ``max_bytes``.

    Returns the text and whether it was cut. A multi-byte character that would
    straddle the limit is dropped rather than split. A character UTF-8 can't encode,
    such as a lone surrogate from decoded JSON, becomes ``?``, so this never raises.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8"), False
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True
