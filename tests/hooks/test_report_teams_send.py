"""Tests for scripts/hooks/report_teams_send.py.

PostToolUse hook for ``mcp__entrabot__send_teams_message``. The previous
implementation was a static ``echo`` that ALWAYS injected "Teams message sent."
as additionalContext — even when the send actually failed (e.g. the placeholder
gate's ``MissingPlaceholderError``, which ``send_teams_message`` returns as
``{"error": ..., "error_type": "MissingPlaceholderError"}``). That misreports a
failed send as a success.

This hook inspects the tool_response and only reports "sent" when the result has
no error; on an error result it says the message was NOT sent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "report_teams_send.py"


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=10,
    )


def _context(result: subprocess.CompletedProcess) -> str:
    """Extract the additionalContext string from the hook's stdout JSON."""
    out = json.loads(result.stdout)
    return out["hookSpecificOutput"]["additionalContext"]


# ── success reports "sent" ───────────────────────────────────────────────────
def test_success_dict_reports_sent():
    r = _run_hook(
        {
            "tool_name": "mcp__entrabot__send_teams_message",
            "tool_response": {"message_id": "1782404793575", "content_type": "html"},
        }
    )
    assert r.returncode == 0, r.stderr
    ctx = _context(r).lower()
    assert "sent" in ctx
    assert "not sent" not in ctx


def test_success_wrapped_result_string_reports_sent():
    # entrabot MCP results often arrive wrapped as {"result": "<json string>"}.
    r = _run_hook(
        {
            "tool_name": "mcp__entrabot__send_teams_message",
            "tool_response": {"result": json.dumps({"message_id": "123"})},
        }
    )
    assert r.returncode == 0, r.stderr
    assert "not sent" not in _context(r).lower()


# ── failure does NOT report "sent" (the bug) ─────────────────────────────────
def test_missing_placeholder_error_is_not_reported_as_sent():
    err = json.dumps(
        {
            "error": "substantive message requires a recent post_thinking_placeholder",
            "error_type": "MissingPlaceholderError",
            "remediation": "post_thinking_placeholder + retry",
        }
    )
    r = _run_hook(
        {
            "tool_name": "mcp__entrabot__send_teams_message",
            "tool_response": err,  # returned as a JSON string
        }
    )
    assert r.returncode == 0, r.stderr
    ctx = _context(r).lower()
    assert "not sent" in ctx
    # Must not claim success.
    assert "message sent." not in ctx


def test_error_inside_wrapped_result_is_detected():
    r = _run_hook(
        {
            "tool_name": "mcp__entrabot__send_teams_message",
            "tool_response": {"result": json.dumps({"error": "chat_id is required"})},
        }
    )
    assert r.returncode == 0, r.stderr
    assert "not sent" in _context(r).lower()


# ── robustness ───────────────────────────────────────────────────────────────
def test_malformed_stdin_fails_open():
    r = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="not json{{",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, r.stderr


def test_empty_falsy_error_key_still_reports_sent():
    # A success result that carries an empty/falsy "error" must not be treated
    # as a failure.
    r = _run_hook(
        {
            "tool_name": "mcp__entrabot__send_teams_message",
            "tool_response": {"message_id": "9", "error": ""},
        }
    )
    assert r.returncode == 0, r.stderr
    assert "not sent" not in _context(r).lower()
