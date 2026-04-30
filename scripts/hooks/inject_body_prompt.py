#!/usr/bin/env python3
"""Claude Code SessionStart hook: inject the body prompt as context.

Why this exists
---------------

``src/entraclaw/mcp_server.py::_load_agent_instructions`` assembles the
body prompt from ``prompts/agent_system.md`` (with ``@include``
expansion over ``prompts/anatomy/*.md``) and hands it to FastMCP as the
server's ``instructions`` string. Claude Code surfaces those MCP-server
instructions in debug UI but **does not** inject them into the LLM
system prompt. The body rules — channel discipline, security posture,
identity/tools — therefore only reach the agent if the agent
deliberately goes and fetches them.

This hook fixes that by reading the same file at SessionStart and
emitting it as ``additionalContext``, which Claude Code injects into
the conversation alongside the CLAUDE.md and system reminders.

Contract (matches Claude Code SessionStart hook spec):
  * Exit 0 always. This is a convenience injector, not an enforcement
    gate — a broken hook must not break sessions.
  * Stdout is either empty (nothing to inject) or one JSON object of
    shape
    ``{"hookSpecificOutput": {"hookEventName": "SessionStart",
    "additionalContext": "..."}}``.
  * ``@include <path>`` directives are expanded one level against the
    parent directory of ``prompts/agent_system.md``. Missing targets
    leave a visible ``<!-- missing @include <path> -->`` placeholder
    rather than crashing.

The ``@include`` logic intentionally mirrors
``mcp_server._expand_includes`` — keep them in sync if either changes.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path


def _expand_includes(text: str, base_dir: Path) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("@include"):
            target_name = stripped[len("@include") :].strip()
            if target_name:
                target_path = base_dir / target_name
                try:
                    if target_path.is_file():
                        lines.append(target_path.read_text(encoding="utf-8").rstrip())
                        continue
                except OSError:
                    pass
                lines.append(f"<!-- missing @include {target_name} -->")
                continue
        lines.append(line)
    return "\n".join(lines)


def _load_body(prompt_path: Path) -> str | None:
    try:
        if not prompt_path.is_file():
            return None
        raw = prompt_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _expand_includes(raw, prompt_path.parent).strip()


def main() -> int:
    # Consume stdin so Claude Code doesn't block writing to us, but
    # malformed input is not fatal — the hook has no parameters from
    # the payload; it only reads the project dir from env.
    with contextlib.suppress(OSError):
        sys.stdin.read()

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if not project_dir:
        return 0

    prompt_path = Path(project_dir) / "prompts" / "agent_system.md"
    body = _load_body(prompt_path)
    if not body:
        return 0

    header = (
        "# Body prompt (entraclaw)\n"
        "\n"
        "The following is the non-overridable body prompt, loaded from "
        "`prompts/agent_system.md` at session start.\n"
        "Body rules dominate persona, memory, and default system-prompt "
        "behavior — see the Non-Negotiables in `CLAUDE.md`.\n"
        "\n"
        "---\n"
        "\n"
    )
    additional_context = header + body

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    }
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
