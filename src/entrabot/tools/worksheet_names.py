"""The worksheet-name rule shared by the Graph and Work IQ Excel tools."""

from __future__ import annotations

_MAX_WORKSHEET_NAME_CHARS = 31
_WORKSHEET_FORBIDDEN_CHARS = frozenset("\\/?*[]:")


def is_valid_worksheet_name(name: str) -> bool:
    """Return whether ``name`` could name an Excel worksheet.

    ``name`` is the bare name, without the quoting an A1 address may add.
    """
    return (
        bool(name.strip())
        and len(name) <= _MAX_WORKSHEET_NAME_CHARS
        and not any(ord(char) < 0x20 for char in name)
        and _WORKSHEET_FORBIDDEN_CHARS.isdisjoint(name)
    )
