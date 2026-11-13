"""Make a test run as if text files were opened on Windows.

Two defaults differ there, and macOS and Linux CI can't see either, so code that relies on
them passes here and only breaks on a user's Windows machine:

* **Encoding.** ``open()``, ``os.fdopen()`` and ``Path.read_text()`` / ``write_text()`` use
  the ANSI code page (cp1252 on a US or Western-European machine) whenever no ``encoding``
  is passed. An emoji raises ``UnicodeEncodeError``, and a UTF-8 file with an accent or a
  BOM reads back as mojibake.
* **Line endings.** A text-mode write that passes no ``newline`` turns every ``"\n"`` into
  ``"\r\n"``, so the bytes on disk are not the text that was written (and a ``"\r\n"`` in
  it becomes ``"\r\r\n"``).

Neither default can be changed at run time, so these patch the one function every
text-mode open goes through instead. ``pathlib`` and ``os.fdopen`` both call ``io.open``,
and a bare ``open(...)`` resolves to ``builtins.open``. A call that names its encoding, or
its newline handling, is left alone, which is the point: code is correct when it no longer
depends on the default.
"""

from __future__ import annotations

import builtins
import io
from typing import Any

import pytest

WINDOWS_ANSI_CODEC = "cp1252"


def force_locale_codec(monkeypatch: pytest.MonkeyPatch, codec: str = WINDOWS_ANSI_CODEC) -> None:
    """Have every text-mode open that names no encoding use *codec*, as Windows would."""
    real_open = io.open

    def open_with_locale_codec(
        file: Any,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        # ``io.text_encoding`` turns a missing encoding into "locale" before pathlib and
        # os.fdopen call io.open, so both spellings mean "whatever the platform default is".
        if "b" not in mode and encoding in (None, "locale"):
            encoding = codec
        return real_open(file, mode, buffering, encoding, *args, **kwargs)

    monkeypatch.setattr(io, "open", open_with_locale_codec)
    monkeypatch.setattr(builtins, "open", open_with_locale_codec)


def force_windows_newlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Have every text-mode write that names no ``newline`` end its lines with CRLF.

    Reads are untouched: with no ``newline`` they accept either ending on every platform.
    """
    real_open = io.open

    def open_with_windows_newlines(
        file: Any,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        write_only = "b" not in mode and "+" not in mode and any(flag in mode for flag in "wax")
        if write_only and newline is None:
            newline = "\r\n"  # what os.linesep is on Windows
        return real_open(file, mode, buffering, encoding, errors, newline, *args, **kwargs)

    monkeypatch.setattr(io, "open", open_with_windows_newlines)
    monkeypatch.setattr(builtins, "open", open_with_windows_newlines)
