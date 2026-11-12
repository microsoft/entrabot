"""Tests for ``start-entrabot.cmd``: what a non-developer double-clicks to start the agent.

It checks that setup has been run, finds Claude Code, points Claude Code at a per-user
Git Bash when needed, and starts Claude Code with the Entrabot channel.

Static assertions run everywhere. The executed tests need ``cmd.exe`` and run only on
Windows (the Windows CI job); they use a stand-in ``claude.cmd`` that echoes what it got.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
START_CMD = REPO_ROOT / "start-entrabot.cmd"

LAUNCH = "--dangerously-load-development-channels server:entrabot %*"


def _text() -> str:
    return START_CMD.read_bytes().decode("ascii").replace("\r\n", "\n")


def _code() -> str:
    return "\n".join(
        line for line in _text().splitlines() if not line.strip().lower().startswith("rem")
    )


class TestFileDiscipline:
    def test_is_ascii(self) -> None:
        START_CMD.read_bytes().decode("ascii")

    def test_has_no_labels_goto_or_parenthesized_blocks(self) -> None:
        # Those are the constructs that misbehave when a .cmd arrives with LF endings,
        # and blocks also break on paths that contain parentheses.
        code = _code()
        assert re.search(r"(?im)^\s*:[A-Za-z]", code) is None
        assert "goto" not in code.lower()
        assert "call :" not in code.lower()
        assert re.search(r"(?m)\(\s*$", code) is None
        assert re.search(r"(?m)^\s*\)", code) is None

    def test_gitattributes_pins_cmd_files_to_crlf(self) -> None:
        attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        assert re.search(r"(?m)^\*\.cmd\s+text\s+eol=crlf\s*$", attributes)

    def test_delayed_expansion_is_off_before_the_arguments_are_used(self) -> None:
        # Delayed expansion eats "!" in arguments, so it is scoped to the detection only.
        code = _code()
        on = code.index("setlocal EnableDelayedExpansion")
        off = code.index("endlocal & set")
        assert on < off < code.index(LAUNCH)
        assert code.count("%*") == 1  # the arguments appear only on the launch line

    def test_never_hides_error_output(self) -> None:
        code = _code().lower()
        assert "2>nul" not in code
        assert "2> nul" not in code
        assert "2>&1" not in code


class TestBehaviourAsWritten:
    def test_runs_from_its_own_folder(self) -> None:
        assert 'cd /d "%~dp0"' in _code()

    def test_requires_setup_first(self) -> None:
        code = _code()
        assert r'if not exist ".venv\Scripts\entrabot-mcp.exe"' in code
        assert 'if not exist ".mcp.json"' in code
        assert r"scripts\setup-windows.cmd" in code
        check = code.index(r'if not exist ".venv\Scripts\entrabot-mcp.exe"')
        assert check < code.index(LAUNCH)

    def test_finds_claude_on_path_then_in_the_native_install_folder(self) -> None:
        code = _code()
        on_path = code.index("%%~$PATH:I")
        native = code.index(r"%USERPROFILE%\.local\bin\claude.exe")
        assert on_path < native < code.index(LAUNCH)
        # No external lookup program whose "not found" message would need hiding.
        assert "where " not in code.lower()

    def test_tells_the_user_how_to_get_claude_code(self) -> None:
        assert (
            r"powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithClaudeCode"
            in _code()
        )

    def test_points_claude_at_a_per_user_git_bash_only_when_needed(self) -> None:
        code = _code()
        line = next(
            item for item in code.splitlines() if 'set "CLAUDE_CODE_GIT_BASH_PATH=' in item
        )
        assert "if not defined CLAUDE_CODE_GIT_BASH_PATH" in line
        assert r'if not exist "%ProgramFiles%\Git\bin\bash.exe"' in line
        assert r'if exist "%LOCALAPPDATA%\Programs\Git\bin\bash.exe"' in line
        assert code.index(line) < code.index(LAUNCH)

    def test_starts_claude_with_the_entrabot_channel_and_returns_its_exit_code(self) -> None:
        code = _code()
        assert f'"%EB_CLAUDE%" {LAUNCH}' in code
        launch = code.index(LAUNCH)
        assert 'set "EB_RC=%ERRORLEVEL%"' in code[launch:]
        assert re.search(r"(?m)^exit /b %EB_RC%\s*$", code[launch:])

    def test_uses_call_only_for_a_cmd_shim(self) -> None:
        # npm installs claude.cmd, which needs `call`; the native claude.exe must not get
        # it, because `call` re-reads the line and would mangle "%" and "^" in arguments.
        code = _code()
        assert 'if /i "%EB_CLAUDE:~-4%"==".cmd" set "EB_CALL=call"' in code
        assert f'%EB_CALL% "%EB_CLAUDE%" {LAUNCH}' in code

    def test_pauses_only_after_a_double_click(self) -> None:
        code = _code()
        assert "!CMDCMDLINE!" in code
        assert "SSH_CONNECTION" in code
        assert "WT_SESSION" in code
        for line in code.splitlines():
            if re.search(r"(?i)\bpause\b", line) and "set " not in line.lower():
                assert 'if "%EB_PAUSE%"=="1"' in line, line
        assert "Get-CimInstance" not in code


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs cmd.exe")

FAKE_CLAUDE = (
    "@echo off\r\n"
    "echo ARGS=[%*]\r\n"
    "echo GITBASH=[%CLAUDE_CODE_GIT_BASH_PATH%]\r\n"
    "echo CWD=[%CD%]\r\n"
    "exit /b %FAKE_CLAUDE_EXIT%\r\n"
)


class _Project:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "entrabot"
        self.root.mkdir()
        self.script = self.root / "start-entrabot.cmd"
        shutil.copyfile(START_CMD, self.script)
        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        self.profile = tmp_path / "profile"
        self.profile.mkdir()
        self.local = tmp_path / "local"
        self.local.mkdir()
        self.program_files = tmp_path / "pf"
        self.program_files.mkdir()
        self.elsewhere = tmp_path / "elsewhere"
        self.elsewhere.mkdir()

    def set_up(self) -> None:
        scripts = self.root / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "entrabot-mcp.exe").write_bytes(b"")
        (self.root / ".mcp.json").write_text("{}", encoding="utf-8")

    def add_claude_on_path(self) -> None:
        (self.tools / "claude.cmd").write_bytes(FAKE_CLAUDE.encode("ascii"))

    def run(
        self, *args: str, claude_exit: int = 0, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env = {
            "SystemRoot": system_root,
            "ComSpec": os.environ.get("COMSPEC", rf"{system_root}\System32\cmd.exe"),
            "PATH": rf"{self.tools};{system_root}\System32",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "USERPROFILE": str(self.profile),
            "LOCALAPPDATA": str(self.local),
            "ProgramFiles": str(self.program_files),
            "TEMP": os.environ.get("TEMP", str(self.elsewhere)),
            "FAKE_CLAUDE_EXIT": str(claude_exit),
            # A remote-session marker: the script must never wait for a key in tests.
            "SSH_CONNECTION": "test",
        }
        env.update(extra)
        return subprocess.run(
            [str(self.script), *args],
            cwd=str(self.elsewhere),  # not the project folder: the script must cd itself
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


@windows_only
class TestRunsOnWindows:
    def test_without_setup_it_says_so_and_exits_1(self, tmp_path: Path) -> None:
        project = _Project(tmp_path)
        project.add_claude_on_path()
        result = project.run()
        assert result.returncode == 1
        assert r"scripts\setup-windows.cmd" in result.stdout
        assert "ARGS=" not in result.stdout
        assert result.stderr == ""

    def test_without_claude_code_it_gives_the_helper_command(self, tmp_path: Path) -> None:
        project = _Project(tmp_path)
        project.set_up()
        result = project.run()
        assert result.returncode == 1
        assert r"scripts\prereqs-windows.ps1 -WithClaudeCode" in result.stdout
        assert result.stderr == ""

    def test_starts_claude_from_the_project_folder_with_the_channel(self, tmp_path: Path) -> None:
        project = _Project(tmp_path)
        project.set_up()
        project.add_claude_on_path()
        result = project.run()
        assert result.returncode == 0, result.stdout + result.stderr
        # (No closing bracket: with no extra arguments cmd may keep a trailing space.)
        assert "ARGS=[--dangerously-load-development-channels server:entrabot" in result.stdout
        # The temp folder can be reported in its 8.3 short form, so compare the tail only.
        cwd = next(line for line in result.stdout.splitlines() if line.startswith("CWD=["))
        assert cwd.lower().endswith("\\entrabot]")
        assert result.stderr == ""

    def test_extra_arguments_survive_including_quotes_and_exclamation_marks(
        self, tmp_path: Path
    ) -> None:
        project = _Project(tmp_path)
        project.set_up()
        project.add_claude_on_path()
        result = project.run("--resume", "two words", "wow!")
        assert result.returncode == 0, result.stdout + result.stderr
        channel = "--dangerously-load-development-channels server:entrabot"
        assert f'ARGS=[{channel} --resume "two words" wow!]' in result.stdout

    def test_claudes_exit_code_is_returned(self, tmp_path: Path) -> None:
        project = _Project(tmp_path)
        project.set_up()
        project.add_claude_on_path()
        assert project.run(claude_exit=7).returncode == 7

    def test_falls_back_to_the_native_install_folder(self, tmp_path: Path) -> None:
        project = _Project(tmp_path)
        project.set_up()
        native = project.profile / ".local" / "bin"
        native.mkdir(parents=True)
        # A real program is needed here; any small in-box .exe will do as the stand-in.
        shutil.copyfile(
            Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "where.exe",
            native / "claude.exe",
        )
        result = project.run()
        # where.exe rejects these arguments with a usage error: what matters is that the
        # launcher found and started the program in the native folder instead of giving
        # the "install Claude Code" message.
        assert r"prereqs-windows.ps1 -WithClaudeCode" not in result.stdout

    def test_per_user_git_bash_is_passed_to_claude(self, tmp_path: Path) -> None:
        project = _Project(tmp_path)
        project.set_up()
        project.add_claude_on_path()
        bash = project.local / "Programs" / "Git" / "bin" / "bash.exe"
        bash.parent.mkdir(parents=True)
        bash.write_bytes(b"")
        result = project.run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"GITBASH=[{bash}]".lower() in result.stdout.lower()

    def test_git_bash_under_program_files_needs_no_override(self, tmp_path: Path) -> None:
        project = _Project(tmp_path)
        project.set_up()
        project.add_claude_on_path()
        for base in (project.program_files / "Git", project.local / "Programs" / "Git"):
            (base / "bin").mkdir(parents=True)
            (base / "bin" / "bash.exe").write_bytes(b"")
        result = project.run()
        assert "GITBASH=[]" in result.stdout

    def test_an_existing_override_is_respected(self, tmp_path: Path) -> None:
        project = _Project(tmp_path)
        project.set_up()
        project.add_claude_on_path()
        bash = project.local / "Programs" / "Git" / "bin" / "bash.exe"
        bash.parent.mkdir(parents=True)
        bash.write_bytes(b"")
        result = project.run(CLAUDE_CODE_GIT_BASH_PATH=r"D:\mine\bash.exe")
        assert r"GITBASH=[D:\mine\bash.exe]" in result.stdout
