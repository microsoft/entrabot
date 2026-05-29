"""Tests for scripts/hooks/require_body_prompt.py.

The hook is a Claude Code PreToolUse hook that blocks high-blast-radius
entrabot tools (send_teams_message, send_email, etc.) unless the transcript
contains evidence that the model loaded the body prompt this session.

Acceptable sentinels:
  1. A Read tool call targeting prompts/agent_system.md or prompts/anatomy/*.md
  2. A successful mcp__persona-sati__get_system_prompt tool call
  3. A successful mcp__persona-sati__bootstrap_session tool result with
     mind_contract_available: true
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "require_body_prompt.py"


def _run_hook(payload: dict, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """Run the hook script with the given JSON payload on stdin."""
    env = dict(os.environ)
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


def _make_transcript_file(entries: list[dict]) -> str:
    """Write a JSONL transcript file to a temp location and return its path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        os.unlink(path)
        raise
    return path


class TestAllow:
    def test_non_gated_tool_is_allowed(self):
        result = _run_hook({"tool_name": "Read", "transcript_path": "/dev/null"})
        assert result.returncode == 0, result.stderr

    def test_read_of_body_prompt_allows_send_teams_message(self):
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": str(REPO_ROOT / "prompts/agent_system.md")},
                        }
                    ],
                }
            }
        ])
        try:
            result = _run_hook(
                {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": transcript}
            )
            assert result.returncode == 0, result.stderr
        finally:
            os.unlink(transcript)

    def test_read_of_anatomy_file_allows_send_teams_message(self):
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {
                                "file_path": str(
                                    REPO_ROOT / "prompts/anatomy/channel-discipline.md"
                                )
                            },
                        }
                    ],
                }
            }
        ])
        try:
            result = _run_hook(
                {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": transcript}
            )
            assert result.returncode == 0, result.stderr
        finally:
            os.unlink(transcript)

    def test_windows_path_read_allows_send_teams_message(self):
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {
                                "file_path": (
                                    r"D:\a\entrabot-identity-research"
                                    r"\prompts\agent_system.md"
                                )
                            },
                        }
                    ],
                }
            }
        ])
        try:
            result = _run_hook(
                {
                    "tool_name": "mcp__entrabot__send_teams_message",
                    "transcript_path": transcript,
                }
            )
            assert result.returncode == 0, result.stderr
        finally:
            os.unlink(transcript)

    def test_get_system_prompt_allows_send_teams_message(self):
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__persona-sati__get_system_prompt",
                            "input": {},
                        }
                    ],
                }
            }
        ])
        try:
            result = _run_hook(
                {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": transcript}
            )
            assert result.returncode == 0, result.stderr
        finally:
            os.unlink(transcript)

    def test_bootstrap_session_with_mind_available_allows_send_teams_message(self):
        """Successful bootstrap_session result with mind_contract_available=true is a sentinel."""
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "mcp__persona-sati__bootstrap_session",
                            "input": {},
                        }
                    ],
                }
            },
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": json.dumps({
                                "schema_version": "1.0",
                                "mind_contract_available": True,
                                "mind_contract": "I am the agent...",
                                "context": {},
                                "memory_catalog": {"total_count": 5},
                            }),
                        }
                    ],
                }
            },
        ])
        try:
            result = _run_hook(
                {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": transcript}
            )
            assert result.returncode == 0, result.stderr
        finally:
            os.unlink(transcript)

    def test_env_override_allows_bypass(self):
        result = _run_hook(
            {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": "/dev/null"},
            env_overrides={"ENTRABOT_SKIP_BODY_PROMPT_GATE": "true"},
        )
        assert result.returncode == 0, result.stderr


class TestBlock:
    def test_no_sentinel_blocks_send_teams_message(self):
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                }
            }
        ])
        try:
            result = _run_hook(
                {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": transcript}
            )
            assert result.returncode == 2
            assert "blocked:" in result.stderr
            decision = json.loads(result.stdout)
            assert decision["decision"] == "block"
            assert "Body prompt gate" in decision["reason"]
        finally:
            os.unlink(transcript)

    def test_bootstrap_session_tool_use_only_still_blocks(self):
        """Seeing only the tool_use without a successful result does not unlock."""
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "mcp__persona-sati__bootstrap_session",
                            "input": {},
                        }
                    ],
                }
            }
        ])
        try:
            result = _run_hook(
                {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": transcript}
            )
            assert result.returncode == 2
            assert "blocked:" in result.stderr
            decision = json.loads(result.stdout)
            assert decision["decision"] == "block"
        finally:
            os.unlink(transcript)

    def test_malformed_bootstrap_result_still_blocks(self):
        """Non-JSON bootstrap_session result does not unlock."""
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "mcp__persona-sati__bootstrap_session",
                            "input": {},
                        }
                    ],
                }
            },
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": "malformed-not-json",
                        }
                    ],
                }
            },
        ])
        try:
            result = _run_hook(
                {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": transcript}
            )
            assert result.returncode == 2
        finally:
            os.unlink(transcript)

    def test_mind_contract_available_false_still_blocks(self):
        """bootstrap_session result with mind_contract_available=false does not unlock."""
        transcript = _make_transcript_file([
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "mcp__persona-sati__bootstrap_session",
                            "input": {},
                        }
                    ],
                }
            },
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": json.dumps({
                                "schema_version": "1.0",
                                "mind_contract_available": False,
                                "degraded_mode": "persona_unreachable",
                            }),
                        }
                    ],
                }
            },
        ])
        try:
            result = _run_hook(
                {"tool_name": "mcp__entrabot__send_teams_message", "transcript_path": transcript}
            )
            assert result.returncode == 2
        finally:
            os.unlink(transcript)

    def test_oversized_transcript_fails_closed(self):
        """Transcripts exceeding _MAX_TRANSCRIPT_BYTES are treated as no body load.
        
        This is a security boundary: if the transcript is too large to scan in
        reasonable time, the hook must fail closed (block) rather than fail open
        (allow). Even if the transcript contains a valid sentinel, the size check
        happens first.
        """
        # Import the hook module to access _transcript_has_body_load directly
        import importlib.util
        spec = importlib.util.spec_from_file_location("require_body_prompt", str(HOOK_SCRIPT))
        hook_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_module)
        
        # Create a transcript with a valid sentinel (Read of agent_system.md)
        transcript_entries = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": str(REPO_ROOT / "prompts/agent_system.md")},
                        }
                    ],
                }
            }
        ]
        transcript_path = _make_transcript_file(transcript_entries)
        
        try:
            # Verify the sentinel is recognized normally
            assert hook_module._transcript_has_body_load(transcript_path)
            
            # Monkeypatch _MAX_TRANSCRIPT_BYTES to be smaller than our transcript
            transcript_size = Path(transcript_path).stat().st_size
            patched_limit = transcript_size - 1  # Just under the actual size
            
            with mock.patch.object(hook_module, '_MAX_TRANSCRIPT_BYTES', patched_limit):
                # Now the same transcript should be rejected due to size
                assert not hook_module._transcript_has_body_load(transcript_path), (
                    "Oversized transcript should fail closed (return False) even with "
                    "valid sentinel present"
                )
            
            # Verify via end-to-end hook invocation as well
            with mock.patch.object(hook_module, '_MAX_TRANSCRIPT_BYTES', patched_limit):
                # Re-exec the module to pick up the patched value in the subprocess
                # Actually, subprocess won't see the monkeypatch. We need to test the
                # function directly, which we did above. The subprocess test would
                # require env var injection or modifying the file, which is too invasive.
                # The direct function test is sufficient for coverage.
                pass
            
        finally:
            os.unlink(transcript_path)
