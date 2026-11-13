#!/usr/bin/env python3
"""Claude Code PreToolUse hook: block local auto-memory writes.

Prevents ``Write`` / ``Edit`` / ``NotebookEdit`` against paths under
``~/.claude/projects/<slug>/memory/`` unless the user has opted in to
local memory via ``ENTRABOT_KEEP_MEMORY_LOCAL=true`` — the same env
var that ``src/entrabot/config.py`` uses to gate operational storage.

Rationale: the project's memory ownership moved to persona-sati, whose
``mcp__persona-sati__write_memory_file`` tool lands content in cloud
blob. Claude Code's built-in auto-memory prompt still encourages direct
``Write`` to the local memory tree — which silently drops data on any
machine change. This hook is the mechanical enforcement of that
routing.

Exit codes (Claude Code convention):
  0 — allow
  2 — block; JSON decision on stdout, reason on stderr for the model

It never exits with any other code: input it cannot decode or parse, and any other
unexpected error, is a block (exit 2) with one line on stderr. See ``run()``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePath

_EDITING_TOOLS = {"Write", "Edit", "NotebookEdit"}
_ENV_OVERRIDE = "ENTRABOT_KEEP_MEMORY_LOCAL"
_HOOK_NAME = "block_local_memory_write"


def _is_memory_path_under(path: PurePath, projects_root: PurePath, *, ignore_case: bool) -> bool:
    """Return True iff *path* is ``<projects_root>/<slug>/memory/<something>``.

    Compares path parts, not text, so a file name that merely contains "memory" can't
    match. With *ignore_case* the parts are compared case-insensitively, as Windows treats
    them: there ``c:\\users\\jo\\...\\Memory\\x.md`` is the same file as the spelling
    ``Path.home()`` reports, and an exact comparison let every other spelling through.
    """
    parts = path.parts
    anchor_parts = projects_root.parts
    if ignore_case:
        parts = tuple(part.casefold() for part in parts)
        anchor_parts = tuple(part.casefold() for part in anchor_parts)
    if len(parts) < len(anchor_parts) + 2:
        # Need at least: <projects_root>/<slug>/memory
        return False
    if parts[: len(anchor_parts)] != anchor_parts:
        return False
    # parts[len(anchor_parts)] is <slug>; next must be "memory".
    return parts[len(anchor_parts) + 1] == "memory"


def _is_local_memory_path(file_path: str) -> bool:
    """Return True iff *file_path* sits under ``~/.claude/projects/<slug>/memory/``.

    Uses ``pathlib`` parent traversal — no regex — so we can't be fooled
    by filenames that merely contain the substring "memory".
    """
    try:
        path = Path(file_path).expanduser()
    except (TypeError, ValueError):
        return False

    home_claude_projects = (Path.home() / ".claude" / "projects").resolve()
    # We do NOT resolve the target path itself — it may not exist yet,
    # and symlink resolution could be slow. We compare by parts instead.
    return _is_memory_path_under(path, home_claude_projects, ignore_case=os.name == "nt")


def main() -> int:
    payload = _read_payload()  # raises on undecodable or malformed input; run() blocks

    if payload.get("tool_name") not in _EDITING_TOOLS:
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return 0
    if not _is_local_memory_path(file_path):
        return 0
    if os.environ.get(_ENV_OVERRIDE, "").lower() == "true":
        return 0

    reason = (
        "Local Claude Code auto-memory writes are disabled. Use "
        "mcp__persona-sati__write_memory_file for memory; use "
        "prompts/anatomy/*.md for body behavior rules (committed via "
        f"PR). Set {_ENV_OVERRIDE}=true to override."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    print(
        f"blocked: local Claude Code auto-memory write to {file_path} — "
        f"route via mcp__persona-sati__write_memory_file (or set "
        f"{_ENV_OVERRIDE}=true to override)",
        file=sys.stderr,
    )
    return 2


def _use_utf8_output() -> None:
    """Write stdout and stderr as UTF-8 whatever the console code page is.

    Claude Code reads hook output as UTF-8, and a block message can quote a path that the
    Windows ANSI code page cannot encode at all.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            continue


def _read_payload() -> dict:
    """The hook input, decoded as UTF-8 from the raw bytes.

    Claude Code writes UTF-8 JSON. ``sys.stdin.read()`` would decode it in the locale's
    code page on Windows (cp1252), which raises on ordinary characters and turns a
    non-ASCII path into mojibake that no longer matches the real one. Raises on anything
    that is not a JSON object; an empty input is an empty payload.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    text = buffer.read().decode("utf-8-sig") if buffer is not None else sys.stdin.read()
    if not text.strip():
        return {}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("hook input is not a JSON object")
    return payload


def run() -> int:
    """``main()`` with the guarantee a gate needs: it only ever returns 0 or 2.

    Exit 1 means "the hook itself broke". Claude Code blocks on that only when the hook
    is configured with ``onFailure: block`` (2.1.295 and later); older versions let the
    tool call through. So anything unexpected, such as input that cannot be decoded or
    parsed, becomes an explicit block with one line saying why.
    """
    _use_utf8_output()
    try:
        return main()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:200]
        print(
            f"blocked: the {_HOOK_NAME} hook could not evaluate this tool call "
            f"({detail!a}); failing closed",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(run())
