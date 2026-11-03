"""The worksheet-name and cell rules shared by the Graph and Work IQ Excel tools."""

from __future__ import annotations

import re

MAX_WORKBOOK_COLUMN = 16_384  # XFD
MAX_WORKBOOK_ROW = 1_048_576

_CELL_REFERENCE_RE = re.compile(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})")
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


def column_number(letters: str) -> int:
    """Return the 1-based column number for column letters such as ``XFD``."""
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def is_valid_cell_reference(reference: str) -> bool:
    """Return whether ``reference`` is one cell that exists on a sheet, such as ``B5``."""
    match = _CELL_REFERENCE_RE.fullmatch(reference)
    if match is None:
        return False
    letters, row = match.groups()
    return column_number(letters) <= MAX_WORKBOOK_COLUMN and int(row) <= MAX_WORKBOOK_ROW
