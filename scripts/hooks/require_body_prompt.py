#!/usr/bin/env python3
"""Claude Code PreToolUse hook: gate high-blast-radius entrabot tools on body-prompt load.

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

Override: ``ENTRABOT_SKIP_BODY_PROMPT_GATE=true`` for emergency
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
    "mcp__entrabot__send_email",
    "mcp__entrabot__send_teams_message",
    "mcp__entrabot__send_card",
    "mcp__entrabot__add_teams_member",
    "mcp__entrabot__create_chat",
    "mcp__entrabot__delete_teams_message",
}
_ENV_OVERRIDE = "ENTRABOT_SKIP_BODY_PROMPT_GATE"
_PERSONA_SENTINEL = "mcp__persona-sati__get_system_prompt"
_BOOTSTRAP_SENTINEL = "mcp__persona-sati__bootstrap_session"
_BODY_FILE_SUFFIX = "prompts/agent_system.md"
_ANATOMY_DIR_FRAGMENT = "prompts/anatomy/"
_MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024  # 50 MB ceiling — bounds worst-case scan


def _transcript_has_body_load(transcript_path: str) -> bool:
    """Return True iff the transcript contains a body-prompt sentinel.
    
    Acceptable sentinels:
      1. Read of prompts/agent_system.md or prompts/anatomy/*.md
      2. mcp__persona-sati__get_system_prompt tool_use
      3. Successful mcp__persona-sati__bootstrap_session tool result with
         mind_contract_available: true
    """
    p = Path(transcript_path)
    if not p.is_file():
        return False
    try:
        if p.stat().st_size > _MAX_TRANSCRIPT_BYTES:
            return False
    except OSError:
        return False

    # Track bootstrap_session tool_use IDs so we can validate their results
    bootstrap_tool_ids: set[str] = set()

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
                    
                    # Check for tool_use sentinels
                    if block.get("type") == "tool_use":
                        name = block.get("name", "")
                        if name == _PERSONA_SENTINEL:
                            return True
                        if name == _BOOTSTRAP_SENTINEL:
                            # Remember this tool_use ID to validate its result later
                            tool_id = block.get("id")
                            if tool_id:
                                bootstrap_tool_ids.add(tool_id)
                        if name == "Read":
                            fp = str(
                                (block.get("input") or {}).get("file_path", "")
                            ).replace("\\", "/")
                            if fp.endswith(_BODY_FILE_SUFFIX) or _ANATOMY_DIR_FRAGMENT in fp:
                                return True
                    
                    # Check for bootstrap_session tool_result
                    elif block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        if tool_use_id in bootstrap_tool_ids:
                            # Parse the result and check mind_contract_available
                            content_str = block.get("content", "")
                            if isinstance(content_str, str):
                                try:
                                    result = json.loads(content_str)
                                    if (
                                        isinstance(result, dict)
                                        and result.get("mind_contract_available") is True
                                    ):
                                        return True
                                except json.JSONDecodeError:
                                    pass
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
        f"  - Call mcp__persona-sati__bootstrap_session() (if persona-sati is "
        f"connected) and receive a result with mind_contract_available: true\n"
        f"  - Call mcp__persona-sati__get_system_prompt() (if persona-sati is "
        f"connected, compatibility path)\n\n"
        f"Emergency bypass: set {_ENV_OVERRIDE}=true in the MCP server "
        f"environment. Use only when you've read the body prompt out-of-band "
        f"(e.g. a sub-agent that won't appear in this transcript)."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    print(
        f"blocked: {tool_name} — body prompt not loaded this session. "
        f"Read prompts/agent_system.md, call {_BOOTSTRAP_SENTINEL}, "
        f"or call {_PERSONA_SENTINEL} first.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
