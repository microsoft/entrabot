"""``start-entrabot.sh``: what starts the agent on macOS and Linux.

Run for real under bash in a throwaway folder, with Claude Code replaced by a stub that
records how it was started.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a bash script for macOS and Linux")

REPO_ROOT = Path(__file__).resolve().parents[2]
START_SH = REPO_ROOT / "start-entrabot.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")

STUB_CLAUDE = """#!/bin/sh
{
    printf 'cwd=%s\\n' "$(pwd)"
    for argument in "$@"; do printf 'arg=%s\\n' "$argument"; done
} > "$STUB_LOG"
exit "${STUB_EXIT:-0}"
"""


def _executable(path: Path, text: str = "#!/bin/sh\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Folder:
    def __init__(self, tmp_path: Path, *, set_up: bool = True) -> None:
        self.root = tmp_path / "My Projects" / "entrabot"
        self.root.mkdir(parents=True)
        shutil.copy(START_SH, self.root / "start-entrabot.sh")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "claude.log"
        if set_up:
            _executable(self.root / ".venv" / "bin" / "entrabot-mcp")
            (self.root / ".mcp.json").write_text("{}", encoding="utf-8")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the repository's own script, by full path
            [BASH, str(self.root / "start-entrabot.sh"), *args],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOME": str(self.home),
                "STUB_LOG": str(self.log),
            }
            | env,
            cwd=self.home,  # started from somewhere else, as from a Finder alias or a path
            stdin=subprocess.DEVNULL,
            check=False,
        )

    def started(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


def test_it_starts_claude_code_in_the_project_folder_with_the_channel(tmp_path: Path) -> None:
    folder = Folder(tmp_path)
    _executable(folder.bin / "claude", STUB_CLAUDE)

    result = folder.run()

    assert result.returncode == 0, result.stderr
    assert folder.started() == [
        f"cwd={folder.root.resolve()}",
        "arg=--dangerously-load-development-channels",
        "arg=server:entrabot",
    ]
    assert "Then say:  Who are you?" in result.stdout


def test_extra_arguments_are_passed_on_exactly(tmp_path: Path) -> None:
    folder = Folder(tmp_path)
    _executable(folder.bin / "claude", STUB_CLAUDE)

    folder.run("--resume", "a b", "$(touch pwned)")

    assert folder.started()[-3:] == ["arg=--resume", "arg=a b", "arg=$(touch pwned)"]
    assert not (folder.root / "pwned").exists() and not (folder.home / "pwned").exists()


@pytest.mark.parametrize("place", [".local/bin/claude", ".claude/local/claude"])
def test_claude_code_is_found_where_its_installer_puts_it(tmp_path: Path, place: str) -> None:
    folder = Folder(tmp_path)
    _executable(folder.home / place, STUB_CLAUDE)

    assert folder.run().returncode == 0
    assert folder.started()[1:] == [
        "arg=--dangerously-load-development-channels",
        "arg=server:entrabot",
    ]


def test_claude_codes_exit_code_is_the_scripts(tmp_path: Path) -> None:
    folder = Folder(tmp_path)
    _executable(folder.bin / "claude", STUB_CLAUDE)

    assert folder.run(STUB_EXIT="7").returncode == 7


def test_a_folder_that_is_not_set_up_says_to_run_setup(tmp_path: Path) -> None:
    folder = Folder(tmp_path, set_up=False)
    _executable(folder.bin / "claude", STUB_CLAUDE)

    result = folder.run()

    assert result.returncode == 1
    assert "./scripts/setup.sh" in result.stderr and folder.started() == []


def test_no_claude_code_says_where_to_get_it(tmp_path: Path) -> None:
    result = Folder(tmp_path).run()

    assert result.returncode == 1
    assert "Claude Code is not installed" in result.stderr
    assert "https://code.claude.com/docs/en/setup" in result.stderr


def test_the_script_is_executable_and_valid() -> None:
    assert os.access(START_SH, os.X_OK)
    check = subprocess.run(  # noqa: S603
        [BASH, "-n", str(START_SH)], capture_output=True, text=True, check=False
    )
    assert check.returncode == 0, check.stderr


def test_the_wizard_and_the_script_agree_on_where_claude_code_is_looked_for() -> None:
    from entrabot.winsetup import hosts

    text = START_SH.read_text(encoding="utf-8")
    assert "$HOME/.local/bin/claude" in text and "$HOME/.claude/local/claude" in text
    assert hosts.START_ARGUMENTS in text
    assert hosts.CLAUDE_URL in text
