"""Tests for the worksheet-name rule shared by the Graph and Work IQ Excel tools."""

from __future__ import annotations

import pytest

from entrabot.tools.worksheet_names import is_valid_cell_reference, is_valid_worksheet_name


@pytest.mark.parametrize(
    "name",
    ["Sheet1", "My Sheet", "a!b", "O'Brien", "x" * 31, "Résumé"],
)
def test_accepts_excel_names(name: str) -> None:
    assert is_valid_worksheet_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "x" * 32,
        "a\nb",
        "a\tb",
        "a\\b",
        "a/b",
        "a?b",
        "a*b",
        "a[b",
        "a]b",
        "a:b",
    ],
)
def test_rejects_names_excel_forbids(name: str) -> None:
    assert is_valid_worksheet_name(name) is False


@pytest.mark.parametrize(
    "reference",
    ["A1", "a1", "xfd1", "XFD1048576", "Z99", "AA10"],
)
def test_accepts_cells_inside_the_sheet(reference: str) -> None:
    assert is_valid_cell_reference(reference) is True


@pytest.mark.parametrize(
    "reference",
    [
        "XFE1",  # one column past XFD
        "ZZZ1",
        "A1048577",  # one row past the last
        "A9999999",
        "A0",
        "A",
        "1",
        "1A",
        "$A$1",
        "A1:B2",
        "A1\n",
        "",
        "A\u0661",  # Arabic-Indic digit one
        "A1\u0661",
        "\u00c91",
    ],
)
def test_rejects_cells_outside_the_sheet_or_malformed(reference: str) -> None:
    assert is_valid_cell_reference(reference) is False
