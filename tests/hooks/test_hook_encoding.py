"""The Claude Code hooks must read their input as UTF-8 and the gates must fail closed.

Claude Code writes the hook payload as UTF-8 JSON. Python on Windows decodes piped
stdin in the ANSI code page (cp1252) unless UTF-8 mode is on, which had three effects:

* a payload containing a character such as a curly quote or an emoji raised
  ``UnicodeDecodeError``; the hook exited 1, which is neither "allow" nor "block";
* a non-ASCII profile path (``C:\\Users\\José``) arrived as mojibake, never matched
  ``Path.home()``, and the memory-write gate ALLOWED the write;
* the same user's ``transcript_path`` was mojibake, so the send gate blocked forever.

These tests force the stdin codec to cp1252 (``PYTHONIOENCODING``) with UTF-8 mode
explicitly off, which reproduces the Windows behaviour on every platform.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "scripts" / "hooks"
BLOCK_MEMORY = HOOKS / "block_local_memory_write.py"
REQUIRE_BODY = HOOKS / "require_body_prompt.py"
INJECT_BODY = HOOKS / "inject_body_prompt.py"

# A curly quote, a cross mark, a thumbs-up and an accented capital: none are ASCII, the
# emoji are not in cp1252 at all, and the UTF-8 bytes of the others are invalid cp1252.
NON_ASCII = "\u201d \u274c \U0001f44d \u00c1"

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="no POSIX sh on PATH")


def _ansi_env(home: Path | None = None, **extra: str) -> dict[str, str]:
    """An environment in which Python decodes stdin the way Windows does by default."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252:strict"
    env["PYTHONUTF8"] = "0"
    env.pop("ENTRABOT_KEEP_MEMORY_LOCAL", None)
    env.pop("ENTRABOT_SKIP_BODY_PROMPT_GATE", None)
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
    env.update(extra)
    return env


def _run(script: Path, stdin: bytes, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _payload(**fields: object) -> bytes:
    return json.dumps(fields, ensure_ascii=False).encode("utf-8")


def _transcript_with_body_read(path: Path) -> None:
    entry = {
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/repo/prompts/agent_system.md"},
                }
            ]
        }
    }
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


class TestMemoryWriteGate:
    def test_non_ascii_content_does_not_break_an_ordinary_write(self, tmp_path: Path) -> None:
        stdin = _payload(
            tool_name="Write",
            tool_input={"file_path": str(tmp_path / "notes.md"), "content": NON_ASCII},
        )
        result = _run(BLOCK_MEMORY, stdin, _ansi_env(tmp_path))
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    def test_memory_path_under_a_non_ascii_home_is_still_blocked(self, tmp_path: Path) -> None:
        home = tmp_path / "Jos\u00e9"
        target = home / ".claude" / "projects" / "some-slug" / "memory" / "note.md"
        target.parent.mkdir(parents=True)
        stdin = _payload(tool_name="Write", tool_input={"file_path": str(target)})
        result = _run(BLOCK_MEMORY, stdin, _ansi_env(home))
        assert result.returncode == 2, result.stderr.decode("utf-8", "replace")
        assert json.loads(result.stdout.decode("utf-8"))["decision"] == "block"

    def test_block_message_survives_a_path_no_ansi_code_page_can_print(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "\u65e5\u672c\u8a9e"
        target = home / ".claude" / "projects" / "slug" / "memory" / "\U0001f44d.md"
        target.parent.mkdir(parents=True)
        stdin = _payload(tool_name="Edit", tool_input={"file_path": str(target)})
        result = _run(BLOCK_MEMORY, stdin, _ansi_env(home))
        assert result.returncode == 2
        assert b"Traceback" not in result.stderr
        assert b"blocked:" in result.stderr

    @pytest.mark.parametrize(
        "stdin",
        [b"{not json", b"\xff\xfe\x00garbage", b"[1, 2, 3]", b'"just a string"'],
        ids=["malformed-json", "not-utf8", "json-array", "json-string"],
    )
    def test_input_it_cannot_understand_blocks(self, tmp_path: Path, stdin: bytes) -> None:
        result = _run(BLOCK_MEMORY, stdin, _ansi_env(tmp_path))
        assert result.returncode == 2
        lines = [line for line in result.stderr.decode("utf-8").splitlines() if line.strip()]
        assert len(lines) == 1
        assert "block_local_memory_write" in lines[0]
        assert "Traceback" not in lines[0]

    def test_empty_input_is_still_allowed(self, tmp_path: Path) -> None:
        assert _run(BLOCK_MEMORY, b"", _ansi_env(tmp_path)).returncode == 0

    def test_a_utf8_byte_order_mark_is_accepted(self, tmp_path: Path) -> None:
        stdin = b"\xef\xbb\xbf" + _payload(tool_name="Read", tool_input={})
        assert _run(BLOCK_MEMORY, stdin, _ansi_env(tmp_path)).returncode == 0


class TestSendGate:
    def test_ungated_tool_with_non_ascii_input_is_allowed(self, tmp_path: Path) -> None:
        stdin = _payload(tool_name="mcp__entrabot__whoami", tool_input={"note": NON_ASCII})
        result = _run(REQUIRE_BODY, stdin, _ansi_env(tmp_path))
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    def test_gated_send_with_non_ascii_text_gets_a_real_verdict(self, tmp_path: Path) -> None:
        stdin = _payload(
            tool_name="mcp__entrabot__send_teams_message", tool_input={"content": NON_ASCII}
        )
        result = _run(REQUIRE_BODY, stdin, _ansi_env(tmp_path))
        assert result.returncode == 2
        assert b"body prompt not loaded" in result.stderr  # the gate's verdict, not a crash

    def test_transcript_under_a_non_ascii_profile_unblocks_the_send(self, tmp_path: Path) -> None:
        profile = tmp_path / "Jos\u00e9" / ".claude" / "projects" / "slug"
        profile.mkdir(parents=True)
        transcript = profile / "session.jsonl"
        _transcript_with_body_read(transcript)
        stdin = _payload(
            tool_name="mcp__entrabot__send_teams_message",
            tool_input={"content": NON_ASCII},
            transcript_path=str(transcript),
        )
        result = _run(REQUIRE_BODY, stdin, _ansi_env(tmp_path))
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    @pytest.mark.parametrize(
        "stdin",
        [b"{not json", b"\xff\xfe\x00garbage", b"[1, 2, 3]"],
        ids=["malformed-json", "not-utf8", "json-array"],
    )
    def test_input_it_cannot_understand_blocks(self, tmp_path: Path, stdin: bytes) -> None:
        result = _run(REQUIRE_BODY, stdin, _ansi_env(tmp_path))
        assert result.returncode == 2
        lines = [line for line in result.stderr.decode("utf-8").splitlines() if line.strip()]
        assert len(lines) == 1
        assert "require_body_prompt" in lines[0]

    def test_empty_input_is_still_allowed(self, tmp_path: Path) -> None:
        assert _run(REQUIRE_BODY, b"", _ansi_env(tmp_path)).returncode == 0


class TestGatesNeverExitOne:
    """Exit 1 is "hook error": blocked only by onFailure, and allowed by old Claude Code."""

    @pytest.mark.parametrize("script", [BLOCK_MEMORY, REQUIRE_BODY], ids=lambda p: p.stem)
    def test_an_unexpected_exception_inside_the_gate_blocks(
        self, script: Path, tmp_path: Path
    ) -> None:
        # tool_input is a string, not an object: `.get` on it raises AttributeError.
        # tool_name is unhashable for the set lookup in the other gate: TypeError.
        stdin = _payload(tool_name=["Write"], tool_input="not-an-object")
        if script is BLOCK_MEMORY:
            stdin = _payload(tool_name="Write", tool_input="not-an-object")
        result = _run(script, stdin, _ansi_env(tmp_path))
        assert result.returncode == 2
        assert b"Traceback" not in result.stderr
        assert script.stem.encode() in result.stderr


class TestSessionStartInjectorStaysSoft:
    @pytest.mark.parametrize(
        "stdin",
        [_payload(note=NON_ASCII), b"\xff\xfe\x00garbage", b"{not json"],
        ids=["non-ascii", "not-utf8", "malformed-json"],
    )
    def test_any_input_still_injects_and_exits_0(self, stdin: bytes) -> None:
        env = _ansi_env(CLAUDE_PROJECT_DIR=str(REPO_ROOT))
        result = _run(INJECT_BODY, stdin, env)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        context = json.loads(result.stdout.decode("utf-8"))["hookSpecificOutput"]
        assert "Body prompt" in context["additionalContext"]


@needs_sh
class TestThroughTheLauncher:
    """`run-hook.sh` switches Python to UTF-8 before it starts the hook."""

    def _through_launcher(
        self, hook: str, stdin: bytes, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        assert SH is not None
        env = dict(env)
        env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
        command = f'ENTRABOT_HOOK={hook}; . "$CLAUDE_PROJECT_DIR/scripts/hooks/run-hook.sh"'
        return subprocess.run(
            [SH, "-c", command],
            input=stdin,
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=60,
            check=False,
        )

    def test_launcher_exports_utf8_mode(self) -> None:
        text = (HOOKS / "run-hook.sh").read_text(encoding="ascii")
        exports = text.index("export PYTHONUTF8")
        assert "PYTHONUTF8=1" in text
        assert "PYTHONIOENCODING=utf-8" in text
        assert "export PYTHONIOENCODING" in text
        # Set before the first interpreter is started.
        assert exports < text.index("exec ")

    def test_send_gate_through_the_launcher_with_non_ascii_text(self, tmp_path: Path) -> None:
        stdin = _payload(tool_name="mcp__entrabot__whoami", tool_input={"note": NON_ASCII})
        result = self._through_launcher("require_body_prompt.py", stdin, _ansi_env(tmp_path))
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    def test_memory_gate_through_the_launcher_under_a_non_ascii_home(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "Jos\u00e9"
        target = home / ".claude" / "projects" / "slug" / "memory" / "note.md"
        target.parent.mkdir(parents=True)
        stdin = _payload(tool_name="Write", tool_input={"file_path": str(target)})
        result = self._through_launcher("block_local_memory_write.py", stdin, _ansi_env(home))
        assert result.returncode == 2, result.stderr.decode("utf-8", "replace")

    def test_malformed_input_through_the_launcher_blocks(self, tmp_path: Path) -> None:
        result = self._through_launcher(
            "require_body_prompt.py", b"\xff\xfe{", _ansi_env(tmp_path)
        )
        assert result.returncode == 2
