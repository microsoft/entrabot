"""ANSI color helpers (port of Cli/Ansi.cs).

Respects ``NO_COLOR`` and disables itself when stdout is not a TTY, so piped/captured
output stays clean.
"""

from __future__ import annotations

import os
import sys

# Enabled unless NO_COLOR is set or stdout is redirected.
ENABLED: bool = os.environ.get("NO_COLOR") is None and sys.stdout.isatty()

_CODES = {
    "dim": "2",
    "bold": "1",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    # ENTRABOT wordmark: bright top rows ("_hi"), deeper body, and a dark drop shadow.
    "entra_hi": "38;5;75",
    "entra": "38;5;33",
    "bot_hi": "38;5;213",
    "bot": "38;5;205",
    "shadow": "38;5;238",
}


def _wrap(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if ENABLED else s


def dim(s: str) -> str:
    return _wrap(_CODES["dim"], s)


def bold(s: str) -> str:
    return _wrap(_CODES["bold"], s)


def red(s: str) -> str:
    return _wrap(_CODES["red"], s)


def green(s: str) -> str:
    return _wrap(_CODES["green"], s)


def yellow(s: str) -> str:
    return _wrap(_CODES["yellow"], s)


def blue(s: str) -> str:
    return _wrap(_CODES["blue"], s)


def magenta(s: str) -> str:
    return _wrap(_CODES["magenta"], s)


def cyan(s: str) -> str:
    return _wrap(_CODES["cyan"], s)
