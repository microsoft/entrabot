"""Tests for the worksheet-name rule shared by the Graph and Work IQ Excel tools."""

from __future__ import annotations

import pytest

from entrabot.tools.worksheet_names import is_valid_worksheet_name


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
