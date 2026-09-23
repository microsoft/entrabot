"""Tests for scripts/hooks/block_local_memory_write.py.

The hook is a Claude Code PreToolUse hook that blocks Write/Edit/NotebookEdit
against the local Claude Code auto-memory directory
(``~/.claude/projects/<slug>/memory/**``) unless the user has explicitly
opted in via ``ENTRABOT_KEEP_MEMORY_LOCAL=true`` — the same env var that
gates operational storage mode in ``src/entrabot/config.py``.

We drive the script as a subprocess to validate the stdio contract that
Claude Code relies on (exit code 0 = allow, 2 = block, JSON decision on
stdout, human-readable reason on stderr).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "block_local_memory_write.py"


def _run_hook(payload: dict, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """Run the hook script with the given JSON payload on stdin."""
    # Build a clean env that does NOT inherit ENTRABOT_KEEP_MEMORY_LOCAL
    # from the caller — we want each test to control the env explicitly.
    env = {k: v for k, v in os.environ.items() if k != "ENTRABOT_KEEP_MEMORY_LOCAL"}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _home_memory_path(slug: str = "example", rel: str = "foo.md") -> str:
    return str(Path.home() / ".claude" / "projects" / slug / "memory" / rel)


class TestAllow:
    def test_non_memory_path_with_write_is_allowed(self):
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/foo.md", "content": "hi"},
            }
        )
        assert result.returncode == 0, result.stderr

    def test_bash_tool_with_memory_path_is_allowed(self):
        # Hook should only match against Write/Edit/NotebookEdit.
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": f"echo hi > {_home_memory_path()}"},
            }
        )
        assert result.returncode == 0, result.stderr

    def test_opt_in_allows_memory_write(self):
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": _home_memory_path(), "content": "hi"},
            },
            env_overrides={"ENTRABOT_KEEP_MEMORY_LOCAL": "true"},
        )
        assert result.returncode == 0, result.stderr

    def test_opt_in_is_case_insensitive(self):
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": _home_memory_path(), "content": "hi"},
            },
            env_overrides={"ENTRABOT_KEEP_MEMORY_LOCAL": "TRUE"},
        )
        assert result.returncode == 0, result.stderr

    def test_path_containing_memory_but_not_in_subtree_is_allowed(self):
        # A file that simply contains "memory" in its name but is NOT
        # under ~/.claude/projects/*/memory/ should not be blocked.
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {
                    "file_path": str(Path.home() / "notes" / "memory-notes.md"),
                    "content": "hi",
                },
            }
        )
        assert result.returncode == 0, result.stderr

    def test_claude_projects_non_memory_sibling_is_allowed(self):
        # ~/.claude/projects/example/not-memory/foo.md is adjacent to
        # memory/ but not inside it.
        path = str(Path.home() / ".claude" / "projects" / "example" / "not-memory" / "foo.md")
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": path, "content": "hi"},
            }
        )
        assert result.returncode == 0, result.stderr

    def test_missing_file_path_is_allowed(self):
        # If tool_input lacks a file_path we can't evaluate — allow.
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"content": "hi"},
            }
        )
        assert result.returncode == 0, result.stderr


class TestBlock:
    @pytest.mark.parametrize("tool_name", ["Write", "Edit", "NotebookEdit"])
    def test_blocks_memory_write_for_editing_tools(self, tool_name):
        path = _home_memory_path()
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": {"file_path": path, "content": "hi"},
            }
        )
        assert result.returncode == 2, (
            f"expected block (exit 2), got {result.returncode}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_blocks_nested_memory_path(self):
        path = _home_memory_path(rel="subdir/nested.md")
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": path, "content": "hi"},
            }
        )
        assert result.returncode == 2, result.stderr

    def test_block_emits_expected_json_decision(self):
        path = _home_memory_path()
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": path, "content": "hi"},
            }
        )
        assert result.returncode == 2
        decision = json.loads(result.stdout)
        assert decision["decision"] == "block"
        reason = decision["reason"]
        assert "mcp__persona-sati__write_memory_file" in reason
        assert "ENTRABOT_KEEP_MEMORY_LOCAL" in reason

    def test_block_emits_human_readable_stderr(self):
        path = _home_memory_path()
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": path, "content": "hi"},
            }
        )
        assert result.returncode == 2
        # The model reads stderr on exit 2 — make sure it's actionable.
        assert "blocked" in result.stderr.lower()
        assert "ENTRABOT_KEEP_MEMORY_LOCAL" in result.stderr

    def test_env_var_unset_blocks(self):
        # Belt-and-suspenders: explicit empty env var also blocks.
        path = _home_memory_path()
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": path, "content": "hi"},
            },
            env_overrides={"ENTRABOT_KEEP_MEMORY_LOCAL": ""},
        )
        assert result.returncode == 2, result.stderr

    def test_env_var_other_value_blocks(self):
        # Anything other than "true" should not count as opt-in.
        path = _home_memory_path()
        result = _run_hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": path, "content": "hi"},
            },
            env_overrides={"ENTRABOT_KEEP_MEMORY_LOCAL": "1"},
        )
        assert result.returncode == 2, result.stderr
