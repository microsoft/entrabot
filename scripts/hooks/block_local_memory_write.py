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
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_EDITING_TOOLS = {"Write", "Edit", "NotebookEdit"}
_ENV_OVERRIDE = "ENTRABOT_KEEP_MEMORY_LOCAL"


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
    parts = path.parts
    anchor_parts = home_claude_projects.parts
    if len(parts) < len(anchor_parts) + 2:
        # Need at least: ~/.claude/projects/<slug>/memory/<something>
        return False
    if parts[: len(anchor_parts)] != anchor_parts:
        return False
    # parts[len(anchor_parts)] is <slug>; next must be "memory".
    return parts[len(anchor_parts) + 1] == "memory"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0  # malformed payload — fail open; Claude Code will log it

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


if __name__ == "__main__":
    sys.exit(main())
