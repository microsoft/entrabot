#!/usr/bin/env python3
"""Claude Code PreToolUse hook: gate high-blast-radius entraclaw tools on body-prompt load.

Why this exists
---------------

The SessionStart hook (``inject_body_prompt.py``) reads
``prompts/agent_system.md`` and emits its content as
``additionalContext``. When the body is small enough Claude Code
inlines it; when it isn't (currently ~24KB) the harness persists it to
a file and inlines only a 2KB preview. The model can technically still
fetch the persisted file — but in practice it doesn't, and the safety
rules embedded in the body (audit-before-act, channel discipline,
attribution) get skipped.

This hook is the mechanical fallback. Before any of the gated tools
fire — all of which create state visible to humans outside this
terminal — the hook scans the transcript for evidence that the model
*itself* engaged with the body prompt this session. Two acceptable
sentinels:

  1. A ``Read`` tool call whose ``file_path`` lands on
     ``prompts/agent_system.md`` or any file under ``prompts/anatomy/``.
  2. A ``mcp__persona-sati__get_system_prompt`` tool call.

The SessionStart hook output is NOT a sentinel — that's the exact
failure mode this gate exists to catch.

Override: ``ENTRACLAW_SKIP_BODY_PROMPT_GATE=true`` for emergency
bypass, mirroring ``block_local_memory_write.py``'s convention.

Exit codes (Claude Code convention):
  0 — allow
  2 — block; JSON decision on stdout, reason on stderr for the model
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_GATED_TOOLS = {
    "mcp__entraclaw__send_email",
    "mcp__entraclaw__send_teams_message",
    "mcp__entraclaw__send_card",
    "mcp__entraclaw__add_teams_member",
    "mcp__entraclaw__create_chat",
    "mcp__entraclaw__delete_teams_message",
}
_ENV_OVERRIDE = "ENTRACLAW_SKIP_BODY_PROMPT_GATE"
_PERSONA_SENTINEL = "mcp__persona-sati__get_system_prompt"
_BODY_FILE_SUFFIX = "prompts/agent_system.md"
_ANATOMY_DIR_FRAGMENT = "prompts/anatomy/"
_MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024  # 50 MB ceiling — bounds worst-case scan


def _transcript_has_body_load(transcript_path: str) -> bool:
    """Return True iff the transcript contains a body-prompt sentinel tool_use."""
    p = Path(transcript_path)
    if not p.is_file():
        return False
    try:
        if p.stat().st_size > _MAX_TRANSCRIPT_BYTES:
            return False
    except OSError:
        return False

    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    if name == _PERSONA_SENTINEL:
                        return True
                    if name == "Read":
                        fp = str((block.get("input") or {}).get("file_path", ""))
                        if fp.endswith(_BODY_FILE_SUFFIX) or _ANATOMY_DIR_FRAGMENT in fp:
                            return True
    except OSError:
        return False
    return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0  # malformed payload — fail open; Claude Code will log

    tool_name = payload.get("tool_name")
    if tool_name not in _GATED_TOOLS:
        return 0
    if os.environ.get(_ENV_OVERRIDE, "").lower() == "true":
        return 0

    transcript_path = payload.get("transcript_path")
    if transcript_path and _transcript_has_body_load(transcript_path):
        return 0

    reason = (
        f"Body prompt gate: '{tool_name}' is high-blast-radius (creates state "
        f"visible to humans outside this terminal) and requires that the body "
        f"prompt be explicitly loaded this session before use. The SessionStart "
        f"hook injecting context does NOT count — the model must engage with "
        f"the rules itself.\n\n"
        f"To unblock, do ONE of these, then retry the tool:\n"
        f"  - Read prompts/agent_system.md (and the prompts/anatomy/*.md files "
        f"it @includes)\n"
        f"  - Call mcp__persona-sati__get_system_prompt() (if persona-sati is "
        f"connected)\n\n"
        f"Emergency bypass: set {_ENV_OVERRIDE}=true in the MCP server "
        f"environment. Use only when you've read the body prompt out-of-band "
        f"(e.g. a sub-agent that won't appear in this transcript)."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    print(
        f"blocked: {tool_name} — body prompt not loaded this session. "
        f"Read prompts/agent_system.md or call {_PERSONA_SENTINEL} first.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
