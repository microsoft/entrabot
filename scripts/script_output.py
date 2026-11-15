"""Output for the scripts in ``scripts/`` when someone runs one on its own.

On Windows, a redirected or piped stdout uses the ANSI code page (often cp1252), which has no
"✅" and many other characters the scripts print, so printing one raised UnicodeEncodeError
after the work was done and the script exited 1. The setup wizard runs the scripts with
``PYTHONIOENCODING=utf-8`` and never meets this. Every script's entry point calls
:func:`make_output_safe` first.

Standard library only, and a sibling of the scripts (``from script_output import
make_output_safe``), so using it adds no requirement: a script that ran without the
``entrabot`` package installed still does.
"""

from __future__ import annotations

import contextlib
import sys
from typing import TextIO

# Error handlers that never raise on a character the encoding lacks. Every other handler does:
# "strict", and also "surrogateescape", which is what a redirected stdout has on Windows
# (seen live: cp1252 with surrogateescape still raised on the check mark).
_COPES = frozenset({"replace", "backslashreplace", "xmlcharrefreplace", "namereplace", "ignore"})


def make_output_safe(*streams: TextIO) -> None:
    """Let each stream (stdout and stderr by default) print a character it cannot encode as a
    replacement instead of raising. The encoding is unchanged, so on a UTF-8 terminal or with
    ``PYTHONIOENCODING=utf-8`` the output is exactly what it was. A stream that already
    copes (Python's stderr uses ``backslashreplace``) is left alone."""
    for stream in streams or (sys.stdout, sys.stderr):
        if getattr(stream, "errors", None) in _COPES:
            continue
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
