"""Every Windows script defends itself against PowerShell 7's module paths.

A user can run ``powershell -File scripts\\setup-windows.ps1`` (or any of the others) from a
PowerShell 7 terminal, or through cmd.exe or Python started from one, without going through
``setup-windows.cmd``. Windows PowerShell 5.1 then inherits PowerShell 7's ``PSModulePath``,
resolves its in-box modules to PowerShell 7 versions it cannot load, and fails on the first
``Write-Host`` or ``Get-FileHash`` with "is not recognized" (about_PSModulePath, "Starting
Windows PowerShell from PowerShell 7"; seen on a CI runner). So each script, under Windows
PowerShell only, puts back Windows PowerShell's own default module locations before the
first cmdlet that needs a module.

Static tests run everywhere; the executed ones run under ``pwsh`` (``ENTRABOT_TEST_PWSH`` or
PATH), and on Windows the decisive one starts the real ``powershell.exe`` with PowerShell 7's
module paths.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._powershell import is_windows_powershell, shell_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = [
    REPO_ROOT / "scripts" / "setup-windows.ps1",
    REPO_ROOT / "scripts" / "prereqs-windows.ps1",
    REPO_ROOT / "scripts" / "deploy-windows.ps1",
    REPO_ROOT / "scripts" / "teardown-windows.ps1",
    REPO_ROOT / "status-windows.ps1",
]
by_script = pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)

GUARD = "if ($PSVersionTable.PSVersion.Major -lt 6) {"
# A command name in PowerShell's Verb-Noun form: Write-Host, Join-Path, a function name...
VERB_NOUN = re.compile(r"(?<![\w$.\-])[A-Z][a-z]+-[A-Z][A-Za-z0-9]+\b")
PS7_MODULES = r"C:\Program Files\PowerShell\7\Modules"


def _code(path: Path) -> str:
    """The script with comments and the leading help block removed."""
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _reset_block(code: str) -> str:
    """The guarded reset, from ``if`` to its closing brace at the start of a line."""
    start = code.index(GUARD)
    end = code.index("\n}", start)
    return code[start : end + 2]


def _reset_body(code: str) -> str:
    """What runs under Windows PowerShell: the inside of the guard."""
    block = _reset_block(code)
    return block[len(GUARD) : -1]


def _pwsh() -> str | None:
    return os.environ.get("ENTRABOT_TEST_PWSH") or shutil.which("pwsh")


def _run(shell: str, command: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**env, "POWERSHELL_TELEMETRY_OPTOUT": "1"}
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class TestStatic:
    @by_script
    def test_the_reset_comes_right_after_progress_is_silenced(self, path: Path) -> None:
        code = _code(path)
        silenced = code.index("$ProgressPreference = 'SilentlyContinue'")
        between = code[
            silenced + len("$ProgressPreference = 'SilentlyContinue'") : code.index(GUARD)
        ]
        assert between.strip() == "", between

    @by_script
    def test_the_reset_precedes_the_first_cmdlet_or_function(self, path: Path) -> None:
        code = _code(path)
        block = _reset_block(code)
        first_command = VERB_NOUN.search(code)
        assert first_command is not None
        assert code.index(block) + len(block) <= first_command.start(), first_command.group(0)

    @by_script
    def test_the_reset_itself_uses_no_cmdlet(self, path: Path) -> None:
        # Even Join-Path and New-Object live in modules, which is exactly what is broken.
        body = _reset_body(_code(path))
        assert VERB_NOUN.search(body) is None, VERB_NOUN.search(body)

    @by_script
    def test_the_reset_builds_windows_powershells_own_defaults(self, path: Path) -> None:
        body = _reset_body(_code(path))
        assert "[Environment]::GetFolderPath('MyDocuments')" in body
        assert "[Environment]::GetFolderPath('ProgramFiles')" in body
        assert "'WindowsPowerShell\\Modules'" in body
        assert "[System.IO.Path]::Combine($PSHOME, 'Modules')" in body
        assert "$env:PSModulePath = " in body
        assert "PowerShell\\7" not in body

    def test_every_script_has_the_same_reset(self) -> None:
        blocks = {_reset_block(_code(path)) for path in SCRIPTS}
        assert len(blocks) == 1

    @by_script
    def test_ascii_only(self, path: Path) -> None:
        path.read_bytes().decode("ascii")


class TestHarness:
    @pytest.mark.parametrize(
        "shell",
        [
            r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.EXE",
            "powershell",
            "powershell.exe",
        ],
    )
    def test_windows_powershell_children_get_no_module_path(self, shell: str) -> None:
        env = {"PSModulePath": PS7_MODULES, "psmodulepath": "x", "KEEP": "1"}
        assert is_windows_powershell(shell)
        assert shell_env(shell, env) == {"KEEP": "1"}

    @pytest.mark.parametrize("shell", [r"C:\Program Files\PowerShell\7\pwsh.exe", "/usr/bin/pwsh"])
    def test_powershell_7_children_keep_it(self, shell: str) -> None:
        env = {"PSModulePath": PS7_MODULES, "KEEP": "1"}
        assert not is_windows_powershell(shell)
        assert shell_env(shell, env) == env


@pytest.mark.skipif(not _pwsh(), reason="no PowerShell 7 available")
class TestUnderPowerShell7:
    @by_script
    def test_dot_sourcing_leaves_powershell_7s_module_path_alone(self, path: Path) -> None:
        pwsh = _pwsh()
        assert pwsh
        env = shell_env(pwsh)
        env["EB_SCRIPT"] = str(path)
        result = _run(
            pwsh,
            "$before = $env:PSModulePath; . $env:EB_SCRIPT; "
            "ConvertTo-Json -Compress -InputObject ($before -eq $env:PSModulePath)",
            env,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1]) is True

    def test_the_reset_replaces_an_inherited_powershell_7_path(self) -> None:
        # The body of the guard, run directly: what Windows PowerShell would end up with.
        pwsh = _pwsh()
        assert pwsh
        body = _reset_body(_code(SCRIPTS[0]))
        env = shell_env(pwsh)
        env["PSModulePath"] = PS7_MODULES
        result = _run(
            pwsh,
            f"{body}\nConvertTo-Json -Compress -InputObject @{{ "
            "path = $env:PSModulePath; home = $PSHOME; "
            "docs = [Environment]::GetFolderPath('MyDocuments'); "
            "pf = [Environment]::GetFolderPath('ProgramFiles') }",
            env,
        )
        assert result.returncode == 0, result.stderr
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        entries = seen["path"].split(";")
        assert PS7_MODULES not in entries
        expected = [
            *([os.path.join(seen["docs"], "WindowsPowerShell\\Modules")] if seen["docs"] else []),
            *([os.path.join(seen["pf"], "WindowsPowerShell\\Modules")] if seen["pf"] else []),
            os.path.join(seen["home"], "Modules"),
        ]
        assert entries == expected


@pytest.mark.skipif(sys.platform != "win32", reason="starts the real Windows PowerShell 5.1")
class TestWindowsPowerShellStartedWithPowerShell7Paths:
    """The failure as CI saw it, reproduced on purpose: Windows PowerShell started with the
    module path a PowerShell 7 parent hands down. Each script must still be able to use the
    in-box cmdlets once it has started."""

    @staticmethod
    def _poisoned_env() -> dict[str, str]:
        env = dict(os.environ)
        pwsh = _pwsh()
        if pwsh:  # exactly what a PowerShell 7 terminal passes on
            result = _run(pwsh, "[Console]::Out.Write($env:PSModulePath)", shell_env(pwsh))
            assert result.returncode == 0, result.stderr
            env["PSModulePath"] = result.stdout
        else:
            env["PSModulePath"] = ";".join([PS7_MODULES, env.get("PSModulePath", "")]).strip(";")
        return env

    @by_script
    def test_in_box_cmdlets_work_after_the_script_starts(self, path: Path, tmp_path: Path) -> None:
        powershell = shutil.which("powershell")
        if not powershell:
            pytest.skip("no Windows PowerShell on this machine")
        sample = tmp_path / "sample.txt"
        sample.write_bytes(b"abc")
        env = self._poisoned_env()
        env.update(EB_SCRIPT=str(path), EB_SAMPLE=str(sample))
        # Deliberately NOT shell_env(): this child gets PowerShell 7's paths.
        result = _run(
            powershell,
            "$ErrorActionPreference = 'Stop'; . $env:EB_SCRIPT; "
            "$hash = (Get-FileHash -LiteralPath $env:EB_SAMPLE -Algorithm SHA256).Hash; "
            "Write-Host ('HASH=' + $hash)",
            env,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "is not recognized" not in output
        assert (
            "HASH=BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD" in result.stdout
        )
