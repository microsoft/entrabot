"""Tests for the text limits shared by the Files and Work IQ adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

import entrabot.a365.excel as excel
from entrabot.tools import files
from entrabot.tools.text_limits import (
    DEFAULT_MAX_TEXT_BYTES,
    max_text_bytes,
    truncate_utf8,
)


class TestMaxTextBytes:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENTRABOT_FILES_MAX_TEXT_BYTES", raising=False)

        assert max_text_bytes() == DEFAULT_MAX_TEXT_BYTES == 200_000

    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENTRABOT_FILES_MAX_TEXT_BYTES", "123")

        assert max_text_bytes() == 123

    @pytest.mark.parametrize("raw", ["", "not-a-number"])
    def test_empty_or_unparseable_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("ENTRABOT_FILES_MAX_TEXT_BYTES", raw)

        assert max_text_bytes() == DEFAULT_MAX_TEXT_BYTES


class TestTruncateUtf8:
    def test_short_text_is_unchanged(self) -> None:
        assert truncate_utf8("hello", max_bytes=5) == ("hello", False)

    def test_long_text_is_cut_and_flagged(self) -> None:
        assert truncate_utf8("hello", max_bytes=3) == ("hel", True)

    def test_never_splits_a_multibyte_character(self) -> None:
        text, truncated = truncate_utf8("aé", max_bytes=2)

        assert (text, truncated) == ("a", True)


class TestSharedUse:
    def test_files_uses_the_shared_default(self) -> None:
        assert files.DEFAULT_MAX_TEXT_BYTES == DEFAULT_MAX_TEXT_BYTES

    def test_excel_adapter_does_not_import_the_files_module(self) -> None:
        assert "entrabot.tools.files" not in Path(excel.__file__).read_text()
