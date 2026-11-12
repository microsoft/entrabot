"""Tests for ``scripts/hooks/run-hook.sh`` and the hook wiring in ``.claude/settings.json``.

The Claude Code hooks used to be ``python3 scripts/hooks/<name>.py``. A standard
Windows Python install has no ``python3``, so on Windows every hook failed to start
and the two PreToolUse gates failed OPEN. The launcher resolves an interpreter that
exists on every platform and the gates are configured to fail closed.

The launcher is exercised with a real ``sh`` in temp projects (including a path with
a space, because this repo is routinely checked out under one).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "scripts" / "hooks" / "run-hook.sh"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="no POSIX sh on PATH")
# A shell script cannot stand in for python.exe on real Windows, so the fake-interpreter
# layouts only run where `sh` executes shebang scripts natively.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake interpreters are sh scripts")

# The exact form .claude/settings.json uses: set the hook name, then source the launcher.
COMMAND = 'ENTRABOT_HOOK={name}; . "$CLAUDE_PROJECT_DIR/scripts/hooks/run-hook.sh"'

FAKE_HOOK = """\
import os
import sys

data = sys.stdin.read()
sys.stdout.write("stdin=" + data)
sys.exit(int(os.environ.get("FAKE_HOOK_EXIT", "0")))
"""


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj with space"
    hooks = project / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(LAUNCHER, hooks / "run-hook.sh")
    (hooks / "fake_hook.py").write_text(FAKE_HOOK, encoding="utf-8")
    return project


def _fake_interpreter(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'#!/bin/sh\necho "interp={marker}" >&2\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    project: Path,
    *,
    name: str = "fake_hook.py",
    path: str | None = None,
    hook_exit: int = 0,
    stdin: str = "{}",
    command: str | None = None,
) -> subprocess.CompletedProcess[str]:
    assert SH is not None
    env = {
        "CLAUDE_PROJECT_DIR": str(project),
        "PATH": path if path is not None else os.environ.get("PATH", ""),
        "FAKE_HOOK_EXIT": str(hook_exit),
    }
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return subprocess.run(
        [SH, "-c", command or COMMAND.format(name=name)],
        cwd=str(project.parent),  # not the project root: the launcher must not rely on cwd
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class TestLauncherFile:
    def test_launcher_exists_and_is_lf_only(self) -> None:
        raw = LAUNCHER.read_bytes()
        assert b"\r" not in raw, "run-hook.sh must be LF-only or Git Bash cannot source it"

    def test_launcher_is_ascii(self) -> None:
        LAUNCHER.read_bytes().decode("ascii")

    def test_launcher_has_no_bashisms(self) -> None:
        text = LAUNCHER.read_text(encoding="utf-8")
        assert "[[" not in text
        assert "function " not in text
        assert "local " not in text
        assert "${BASH" not in text

    def test_launcher_never_hides_stderr(self) -> None:
        text = LAUNCHER.read_text(encoding="utf-8")
        assert "2>/dev/null" not in text
        assert "2> /dev/null" not in text


@needs_sh
@posix_only
class TestInterpreterResolution:
    def test_posix_venv_is_preferred(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        _fake_interpreter(project / ".venv" / "bin" / "python3", "posix-venv")
        _fake_interpreter(project / ".venv" / "Scripts" / "python.exe", "windows-venv")
        result = _run(project, stdin='{"a": 1}')
        assert result.returncode == 0, result.stderr
        assert "interp=posix-venv" in result.stderr
        assert result.stdout == 'stdin={"a": 1}'

    def test_windows_venv_layout_is_used_when_posix_layout_is_absent(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        _fake_interpreter(project / ".venv" / "Scripts" / "python.exe", "windows-venv")
        result = _run(project)
        assert result.returncode == 0, result.stderr
        assert "interp=windows-venv" in result.stderr

    def test_falls_back_to_python3_on_path(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        bindir = tmp_path / "bin"
        _fake_interpreter(bindir / "python3", "path-python3")
        result = _run(project, path=str(bindir))
        assert result.returncode == 0, result.stderr
        assert "interp=path-python3" in result.stderr

    def test_falls_back_to_python_on_path(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        bindir = tmp_path / "bin"
        _fake_interpreter(bindir / "python", "path-python")
        result = _run(project, path=str(bindir))
        assert result.returncode == 0, result.stderr
        assert "interp=path-python" in result.stderr

    def test_falls_back_to_py_launcher(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        py = bindir / "py"
        # The launcher must call `py -3 <script>`; drop the -3 and run the script.
        py.write_text(
            f'#!/bin/sh\necho "interp=py-launcher $1" >&2\nshift\nexec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        py.chmod(0o755)
        result = _run(project, path=str(bindir))
        assert result.returncode == 0, result.stderr
        assert "interp=py-launcher -3" in result.stderr

    def test_windowsapps_store_alias_is_rejected(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        stub_dir = tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps"
        _fake_interpreter(stub_dir / "python3", "store-stub")
        _fake_interpreter(stub_dir / "python", "store-stub")
        result = _run(project, path=str(stub_dir))
        assert result.returncode == 2
        assert "interp=store-stub" not in result.stderr
        assert "no Python interpreter" in result.stderr

    def test_no_interpreter_exits_2_with_one_clear_line(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        result = _run(project, path=str(empty))
        assert result.returncode == 2
        assert result.stdout == ""
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        assert len(lines) == 1
        assert "no Python interpreter" in lines[0]
        assert "setup" in lines[0]

    @pytest.mark.parametrize("code", [0, 1, 2])
    def test_hook_exit_code_is_preserved(self, tmp_path: Path, code: int) -> None:
        project = _make_project(tmp_path)
        _fake_interpreter(project / ".venv" / "bin" / "python3", "posix-venv")
        result = _run(project, hook_exit=code)
        assert result.returncode == code

    def test_direct_invocation_with_argument_also_works(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        _fake_interpreter(project / ".venv" / "bin" / "python3", "posix-venv")
        result = _run(
            project,
            command='sh "$CLAUDE_PROJECT_DIR/scripts/hooks/run-hook.sh" fake_hook.py',
            hook_exit=2,
        )
        assert result.returncode == 2
        assert "interp=posix-venv" in result.stderr


@needs_sh
class TestFailsClosed:
    @pytest.mark.parametrize(
        "name",
        ["", "../fake_hook.py", "sub/fake_hook.py", "fake_hook", "fake hook.py", "-c.py"],
    )
    def test_invalid_hook_name_exits_2(self, tmp_path: Path, name: str) -> None:
        project = _make_project(tmp_path)
        result = _run(project, name=f"'{name}'")
        assert result.returncode == 2
        assert "invalid hook name" in result.stderr

    def test_missing_hook_script_exits_2(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        result = _run(project, name="does_not_exist.py")
        assert result.returncode == 2
        assert "hook script not found" in result.stderr

    def test_real_interpreter_runs_real_hook(self, tmp_path: Path) -> None:
        # No fake layout: whatever interpreter this platform really has must work.
        # This is the test that proves the launcher on the Windows CI runner (Git Bash).
        project = _make_project(tmp_path)
        result = _run(project, stdin="hello", hook_exit=2)
        assert result.returncode == 2, result.stderr
        assert result.stdout == "stdin=hello"


def _command_hooks() -> list[tuple[str, dict[str, object]]]:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    found: list[tuple[str, dict[str, object]]] = []
    for event, groups in settings["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                found.append((event, hook))
    return found


def _python_hooks() -> list[tuple[str, dict[str, object]]]:
    return [(e, h) for e, h in _command_hooks() if "run-hook.sh" in str(h["command"])]


class TestSettingsWiring:
    def test_settings_is_valid_json_with_the_three_python_hooks(self) -> None:
        names = sorted(
            re.search(r"ENTRABOT_HOOK=(\S+);", str(hook["command"])).group(1)  # type: ignore[union-attr]
            for _, hook in _python_hooks()
        )
        assert names == [
            "block_local_memory_write.py",
            "inject_body_prompt.py",
            "require_body_prompt.py",
        ]

    def test_no_hook_depends_on_a_bare_python3(self) -> None:
        for _, hook in _command_hooks():
            assert not str(hook["command"]).startswith("python3 ")
            assert not str(hook["command"]).startswith("python ")

    def test_every_referenced_hook_file_exists(self) -> None:
        for _, hook in _command_hooks():
            command = str(hook["command"])
            for rel in re.findall(r"scripts/hooks/[A-Za-z0-9_.-]+", command):
                assert (REPO_ROOT / rel).is_file(), rel
            match = re.search(r"ENTRABOT_HOOK=(\S+);", command)
            if match:
                assert (REPO_ROOT / "scripts" / "hooks" / match.group(1)).is_file()

    def test_python_hooks_are_pinned_to_bash_and_quote_the_project_dir(self) -> None:
        for _, hook in _python_hooks():
            assert hook.get("shell") == "bash"
            assert '"$CLAUDE_PROJECT_DIR/scripts/hooks/run-hook.sh"' in str(hook["command"])
            assert "args" not in hook  # shell form: `shell` is ignored in exec form

    def test_pretooluse_gates_fail_closed(self) -> None:
        gates = [hook for event, hook in _python_hooks() if event == "PreToolUse"]
        assert len(gates) == 2
        for hook in gates:
            assert hook.get("onFailure") == "block"

    def test_launcher_hooks_have_room_for_a_cold_start(self) -> None:
        # With onFailure "block" a timeout blocks the tool call, and a cold Git Bash plus
        # Python start on Windows can take several seconds.
        for _, hook in _python_hooks():
            assert int(hook["timeout"]) >= 20  # type: ignore[call-overload]

    def test_session_start_injector_does_not_block(self) -> None:
        # Exit 2 on SessionStart cannot block anyway; `onFailure` would only add noise.
        injectors = [hook for event, hook in _python_hooks() if event == "SessionStart"]
        assert len(injectors) == 1
        assert "onFailure" not in injectors[0]

    def test_persona_auto_allow_hook_stays_shell_neutral(self) -> None:
        # It is a bare `echo '<json>'`, which Git Bash, sh and PowerShell all run the same
        # way, so it must NOT be pinned to bash (that would break it without Git Bash).
        echoes = [h for _, h in _command_hooks() if str(h["command"]).startswith("echo ")]
        assert len(echoes) == 1
        assert "shell" not in echoes[0]
        payload = str(echoes[0]["command"])[len("echo ") :].strip("'")
        assert json.loads(payload)["hookSpecificOutput"]["permissionDecision"] == "allow"


@needs_sh
class TestSettingsCommandsRunAsWritten:
    """Execute each hook command exactly as Claude Code would: `sh -c <command>`."""

    def _run_as_written(self, command: str, stdin: str) -> subprocess.CompletedProcess[str]:
        assert SH is not None
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
        env.pop("ENTRABOT_SKIP_BODY_PROMPT_GATE", None)
        env.pop("ENTRABOT_KEEP_MEMORY_LOCAL", None)
        return subprocess.run(
            [SH, "-c", command],
            cwd=str(REPO_ROOT),
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def _command_for(self, name: str) -> str:
        for _, hook in _python_hooks():
            if f"ENTRABOT_HOOK={name};" in str(hook["command"]):
                return str(hook["command"])
        raise AssertionError(f"no hook wired for {name}")

    def test_inject_body_prompt_emits_session_context(self) -> None:
        result = self._run_as_written(self._command_for("inject_body_prompt.py"), "{}")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "Body prompt" in payload["hookSpecificOutput"]["additionalContext"]

    def test_block_local_memory_write_allows_an_ordinary_path(self) -> None:
        stdin = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x.txt"}})
        result = self._run_as_written(self._command_for("block_local_memory_write.py"), stdin)
        assert result.returncode == 0, result.stderr

    def test_block_local_memory_write_blocks_the_memory_dir(self) -> None:
        target = Path.home() / ".claude" / "projects" / "some-slug" / "memory" / "note.md"
        stdin = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)}})
        result = self._run_as_written(self._command_for("block_local_memory_write.py"), stdin)
        assert result.returncode == 2, result.stderr

    def test_require_body_prompt_blocks_a_gated_tool_without_a_transcript(self) -> None:
        stdin = json.dumps({"tool_name": "mcp__entrabot__send_teams_message"})
        result = self._run_as_written(self._command_for("require_body_prompt.py"), stdin)
        assert result.returncode == 2, result.stderr

    def test_require_body_prompt_allows_an_ungated_tool(self) -> None:
        stdin = json.dumps({"tool_name": "mcp__entrabot__whoami"})
        result = self._run_as_written(self._command_for("require_body_prompt.py"), stdin)
        assert result.returncode == 0, result.stderr
