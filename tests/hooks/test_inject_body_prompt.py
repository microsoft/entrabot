"""Tests for scripts/hooks/inject_body_prompt.py.

SessionStart hook that reads ``prompts/agent_system.md`` (with
``@include`` expansion of anatomy modules) and emits it as
``additionalContext`` so the body's non-overridable rules land in the
conversation at session boot rather than sitting invisibly in FastMCP
``instructions`` that Claude Code never injects into the system prompt.

Contract validated here:

* Exit 0 always — the hook is a convenience injector, not an
  enforcement gate; failing it must not break sessions.
* Stdout is valid JSON shaped
  ``{"hookSpecificOutput": {"hookEventName": "SessionStart",
  "additionalContext": "..."}}`` whenever a prompt exists to inject.
* ``@include`` directives are expanded one level (same semantics as
  ``mcp_server._expand_includes``).
* Missing ``@include`` targets leave a visible ``<!-- missing ... -->``
  placeholder, never crash.
* Missing body prompt file → no output, exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "inject_body_prompt.py"


def _run_hook(project_dir: Path, payload: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()}
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload or {}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _write_prompt(project_dir: Path, body: str, includes: dict[str, str]) -> None:
    prompts_dir = project_dir / "prompts"
    anatomy_dir = prompts_dir / "anatomy"
    anatomy_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "agent_system.md").write_text(body, encoding="utf-8")
    for name, content in includes.items():
        (anatomy_dir / name).write_text(content, encoding="utf-8")


class TestHookEmitsBodyPrompt:
    def test_hook_emits_expanded_body_as_additional_context(self, tmp_path: Path) -> None:
        _write_prompt(
            tmp_path,
            body=("# body\n\nrule A\n\n@include anatomy/channel.md\n\nrule B\n"),
            includes={"channel.md": "ALWAYS HTML in Teams"},
        )

        result = _run_hook(tmp_path)

        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        hso = parsed["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionStart"
        ctx = hso["additionalContext"]
        assert "rule A" in ctx
        assert "rule B" in ctx
        # Include was expanded.
        assert "ALWAYS HTML in Teams" in ctx
        # Source trail so the reader can find the file.
        assert "prompts/agent_system.md" in ctx

    def test_missing_include_leaves_placeholder_and_still_exits_zero(self, tmp_path: Path) -> None:
        _write_prompt(
            tmp_path,
            body="body rule\n\n@include anatomy/missing.md\n",
            includes={},  # none — @include target absent
        )

        result = _run_hook(tmp_path)

        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "body rule" in ctx
        assert "missing @include" in ctx


class TestHookGracefulDegradation:
    def test_no_prompt_file_exits_zero_with_empty_stdout(self, tmp_path: Path) -> None:
        # No prompts/ directory at all.
        result = _run_hook(tmp_path)
        assert result.returncode == 0, result.stderr
        # No output — Claude Code treats empty stdout as "nothing to inject".
        assert result.stdout.strip() == ""

    def test_claude_project_dir_unset_exits_zero(self, tmp_path: Path) -> None:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="{}",
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        # Nothing to inject.
        assert result.stdout.strip() == ""

    def test_malformed_stdin_payload_does_not_crash(self, tmp_path: Path) -> None:
        _write_prompt(tmp_path, body="ok\n", includes={})
        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="not-json",
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)},
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        # Body was still injected; malformed stdin is ignored.
        parsed = json.loads(result.stdout)
        assert "ok" in parsed["hookSpecificOutput"]["additionalContext"]


class TestHookAgainstRealRepoPrompt:
    """Smoke test against the actual prompts/agent_system.md in the repo.

    This catches regressions where the hook cannot parse the real prompt
    (e.g., if someone introduces a new directive syntax without updating
    the expander).
    """

    @pytest.mark.skipif(
        not (REPO_ROOT / "prompts" / "agent_system.md").is_file(),
        reason="Repo has no prompts/agent_system.md — skip smoke test.",
    )
    def test_real_prompt_expands_and_contains_channel_discipline(self) -> None:
        result = _run_hook(REPO_ROOT)
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        # The anatomy channel-discipline rule must be present once the
        # body prompt is expanded — this is the whole reason the hook
        # exists.
        assert "Always HTML in Teams" in ctx or "HTML in Teams" in ctx
