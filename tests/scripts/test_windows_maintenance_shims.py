"""The three thin Windows wrappers: deploy-windows.ps1, teardown-windows.ps1, status-windows.ps1.

Each maps its parameters to a ``python -m entrabot.winsetup`` subcommand (rotate-cert,
teardown, status), runs it with the project's own interpreter and exits with its exit code.
All the behaviour lives in Python, where it is unit-tested (tests/winsetup). What is tested
here is that the wrappers stay runnable on the Windows PowerShell 5.1 that ships with
Windows, pass the right arguments, and do nothing else.

Two kinds of test, following the repo convention for ``.ps1`` files:

* static text assertions, which run everywhere;
* executed tests under every PowerShell that is available: ``pwsh`` when it is on PATH (or
  named by ``ENTRABOT_TEST_PWSH``) and, on Windows, the in-box ``powershell.exe`` 5.1.
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

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy-windows.ps1"
TEARDOWN = REPO_ROOT / "scripts" / "teardown-windows.ps1"
STATUS = REPO_ROOT / "status-windows.ps1"
SHIMS = [DEPLOY, TEARDOWN, STATUS]
by_name = pytest.mark.parametrize("path", SHIMS, ids=lambda p: p.name)


def _code(path: Path) -> str:
    """The script with comments and the leading help block removed."""
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _shells() -> list[tuple[str, str]]:
    shells: list[tuple[str, str]] = []
    pwsh = os.environ.get("ENTRABOT_TEST_PWSH") or shutil.which("pwsh")
    if pwsh:
        shells.append(("pwsh", pwsh))
    if sys.platform == "win32":
        ps51 = shutil.which("powershell")
        if ps51:
            shells.append(("powershell-5.1", ps51))
    return shells


SHELLS = _shells()
shell_params = (
    [pytest.param(path, id=name) for name, path in SHELLS]
    if SHELLS
    else [pytest.param("", marks=pytest.mark.skip(reason="no PowerShell available"))]
)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="simulated with a fake python")


def _ps(shell: str, script: Path, expression: str) -> object:
    """Dot-source ``script`` and return ``expression`` decoded from JSON."""
    command = (
        "$ErrorActionPreference = 'Stop'; . $env:EB_SCRIPT; "
        f"ConvertTo-Json -Compress -Depth 4 -InputObject ({expression})"
    )
    env = dict(os.environ)
    env["EB_SCRIPT"] = str(script)
    env["EB_ROOT"] = r"C:\Users\Jo O'Brien\entrabot"
    env["POWERSHELL_TELEMETRY_OPTOUT"] = "1"
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    return json.loads(result.stdout.strip().splitlines()[-1])


ROOT = r"C:\Users\Jo O'Brien\entrabot"


# ─────────────────────────────────────────────────────────────────────────────
# Static
# ─────────────────────────────────────────────────────────────────────────────


class TestRunsOnWindowsPowerShell51:
    @by_name
    def test_ascii_only_and_no_bom(self, path: Path) -> None:
        # 5.1 reads a BOM-less file as the ANSI code page, so a single non-ASCII character
        # is a parse error there. These three used to carry a BOM and box-drawing rules.
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        raw.decode("ascii")

    @by_name
    @pytest.mark.parametrize(
        "construct",
        [
            "$IsWindows",  # undefined in 5.1; an error under strict mode
            "$IsLinux",
            "$IsMacOS",
            "$PSNativeCommandUseErrorActionPreference",
            "??",
            "?.",
            " && ",
            " || ",
            "2>$null",  # AGENTS.md: never hide stderr
            "2>&1",  # 5.1 turns native stderr into terminating errors when redirected
            "-o tsv",
            "--output tsv",
            "utf8NoBOM",
            "-AsHashtable",
            "#Requires -Version 7",
            "PSVersion.Major -lt 7",  # the old "needs PowerShell 7" refusal
            "pwsh",  # nothing here may depend on PowerShell 7 being installed
        ],
    )
    def test_no_powershell7_only_or_forbidden_construct(self, path: Path, construct: str) -> None:
        assert construct not in _code(path)

    @by_name
    def test_no_ternary(self, path: Path) -> None:
        assert re.search(r"\)\s\?\s.+\s:\s", _code(path)) is None

    @by_name
    def test_no_assignment_to_an_automatic_variable(self, path: Path) -> None:
        automatic = r"args|input|matches|error|host|home|pid|pwd|profile|psitem|this|event|sender"
        assert re.search(rf"(?im)^\s*\$({automatic})\s*[+]?=", _code(path)) is None

    @by_name
    def test_progress_is_silenced_before_anything_runs(self, path: Path) -> None:
        code = _code(path)
        silenced = code.index("$ProgressPreference = 'SilentlyContinue'")
        assert code.index("param(") < silenced < code.index("function ")


class TestThinWrapper:
    @by_name
    def test_hands_off_to_the_python_command_and_returns_its_exit_code(self, path: Path) -> None:
        code = _code(path)
        assert "$handoff = @('-m', 'entrabot.winsetup') + " in code
        assert "& $venvPython @handoff" in code
        assert code.rstrip().endswith("exit $LASTEXITCODE")

    @by_name
    def test_uses_the_projects_own_interpreter_and_utf8(self, path: Path) -> None:
        code = _code(path)
        assert "'Scripts\\python.exe'" in code and "'.venv'" in code
        assert "$env:PYTHONUTF8 = '1'" in code
        assert "$env:PYTHONIOENCODING = 'utf-8'" in code

    @by_name
    def test_a_folder_that_was_never_set_up_gets_one_clear_line(self, path: Path) -> None:
        code = _code(path)
        assert "Test-Path -LiteralPath $venvPython -PathType Leaf" in code
        assert "scripts\\setup-windows.cmd" in code

    @by_name
    def test_installs_nothing_and_touches_no_certificate_itself(self, path: Path) -> None:
        code = _code(path)
        for forbidden in ("pip", "-m venv", "Cert:", "Remove-Item", "Set-Content", "icacls"):
            assert forbidden not in code
        assert "show_agent_status.py" not in code  # the Python command runs it

    @by_name
    def test_reading_the_exit_code_is_safe_under_strict_mode(self, path: Path) -> None:
        code = _code(path)
        assert code.index("Set-StrictMode -Version Latest") < code.index("$global:LASTEXITCODE = 0")

    def test_deploy_runs_rotate_cert(self) -> None:
        assert "'rotate-cert', '--project-root', $ProjectRoot" in _code(DEPLOY)

    def test_teardown_runs_teardown_and_keeps_the_old_force_switch(self) -> None:
        code = _code(TEARDOWN)
        assert "'teardown', '--project-root', $ProjectRoot" in code
        assert "[switch]$Force" in code and "([bool]$Yes) -or ([bool]$Force)" in code

    def test_teardown_help_says_what_happens_to_the_setup_credential(self) -> None:
        text = TEARDOWN.read_text(encoding="ascii")
        assert ".PARAMETER KeepSetupCredential" in text
        assert "setup (Provisioner) certificate" in text
        assert "[switch]$KeepSetupCredential" in text

    def test_teardown_no_longer_deletes_by_subject_or_wipes_data_itself(self) -> None:
        code = _code(TEARDOWN)
        assert "CN=entrabot-blueprint" not in code
        assert "Get-ChildItem" not in code
        assert "-Recurse" not in code

    def test_status_errors_go_to_the_error_stream_so_json_stays_clean(self) -> None:
        code = _code(STATUS)
        assert "'status', '--project-root', $ProjectRoot" in code
        assert "Write-Host" not in code
        assert code.count("[Console]::Error.WriteLine(") == 2

    def test_status_root_is_the_folder_the_script_is_in(self) -> None:
        code = _code(STATUS)
        assert "$ProjectRoot = Split-Path -Parent $PSCommandPath" in code

    @pytest.mark.parametrize("path", [DEPLOY, TEARDOWN], ids=lambda p: p.name)
    def test_scripts_folder_wrappers_find_the_project_one_level_up(self, path: Path) -> None:
        one_level_up = "$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)"
        assert one_level_up in _code(path)


# ─────────────────────────────────────────────────────────────────────────────
# Executed: parsing and argument mapping
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shell", shell_params)
class TestParses:
    @by_name
    def test_script_parses_without_errors(self, shell: str, path: Path) -> None:
        command = (
            "$errors = $null; $tokens = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$env:EB_SCRIPT, [ref]$tokens, [ref]$errors); "
            "ConvertTo-Json -Compress -InputObject @($errors | ForEach-Object { $_.ToString() })"
        )
        env = dict(os.environ)
        env["EB_SCRIPT"] = str(path)
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", command],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def _args(shell: str, script: Path, call: str) -> list[str]:
    result = _ps(shell, script, f"@({call})")
    assert isinstance(result, list)
    return [str(item) for item in result]


@pytest.mark.parametrize("shell", shell_params)
class TestArguments:
    def test_rotate_default(self, shell: str) -> None:
        call = (
            "Get-RotateArguments -ProjectRoot $env:EB_ROOT -AssumeYes $false -ShowEverything $false"
        )
        assert _args(shell, DEPLOY, call) == ["rotate-cert", "--project-root", ROOT]

    def test_rotate_yes_and_verbose(self, shell: str) -> None:
        call = (
            "Get-RotateArguments -ProjectRoot $env:EB_ROOT -AssumeYes $true -ShowEverything $true"
        )
        assert _args(shell, DEPLOY, call) == [
            "rotate-cert",
            "--project-root",
            ROOT,
            "--yes",
            "--verbose",
        ]

    def test_teardown_default_is_the_safe_scope(self, shell: str) -> None:
        call = (
            "Get-TeardownArguments -ProjectRoot $env:EB_ROOT -DeleteAgent $false "
            "-PurgeData $false -KeepSetupCredential $false -AssumeYes $false "
            "-ShowEverything $false"
        )
        assert _args(shell, TEARDOWN, call) == ["teardown", "--project-root", ROOT]

    def test_teardown_every_scope(self, shell: str) -> None:
        call = (
            "Get-TeardownArguments -ProjectRoot $env:EB_ROOT -DeleteAgent $true "
            "-PurgeData $true -KeepSetupCredential $true -AssumeYes $true "
            "-ShowEverything $true"
        )
        assert _args(shell, TEARDOWN, call) == [
            "teardown",
            "--project-root",
            ROOT,
            "--delete-agent",
            "--purge-data",
            "--keep-setup-credential",
            "--yes",
            "--verbose",
        ]

    def test_status_default(self, shell: str) -> None:
        call = (
            "Get-StatusArguments -ProjectRoot $env:EB_ROOT -Json $false "
            "-HealthOnly $false -Strict $false"
        )
        assert _args(shell, STATUS, call) == ["status", "--project-root", ROOT]

    def test_status_every_flag(self, shell: str) -> None:
        call = (
            "Get-StatusArguments -ProjectRoot $env:EB_ROOT -Json $true "
            "-HealthOnly $true -Strict $true"
        )
        assert _args(shell, STATUS, call) == [
            "status",
            "--project-root",
            ROOT,
            "--json",
            "--health-only",
            "--strict",
        ]

    @by_name
    def test_dot_sourcing_runs_nothing(self, shell: str, path: Path) -> None:
        # The helpers above loaded the file; this proves it stopped at the guard: with no
        # .venv next to the real script, running on would have exited 1 before returning.
        assert _ps(shell, path, "'loaded'") == "loaded"


# ─────────────────────────────────────────────────────────────────────────────
# Executed: the whole wrapper against a stand-in interpreter
# ─────────────────────────────────────────────────────────────────────────────

_FAKE_PYTHON = """#!/bin/sh
{
  printf '%s\\n' "PYTHONUTF8=$PYTHONUTF8" "PYTHONIOENCODING=$PYTHONIOENCODING"
  for arg in "$@"; do printf '%s\\n' "$arg"; done
} > "$FAKE_ARGV"
echo "from the python command"
exit "${FAKE_EXIT:-0}"
"""


class _Folder:
    """A project folder holding a copy of one wrapper and a stand-in for its interpreter."""

    def __init__(self, tmp_path: Path, shell: str, shim: Path, *, installed: bool = True) -> None:
        self.shell = shell
        # A space and an apostrophe: the two characters that used to break these scripts.
        self.root = tmp_path / "it's here" / "entrabot"
        (self.root / "scripts").mkdir(parents=True)
        relative = shim.relative_to(REPO_ROOT)
        self.script = self.root / relative
        self.script.write_bytes(shim.read_bytes())
        shutil.copy(STATUS, self.root / "status-windows.ps1")
        self.argv = tmp_path / "argv.txt"
        if installed:
            python = self.root / ".venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_text(_FAKE_PYTHON, encoding="ascii")
            python.chmod(0o755)

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(OS="Windows_NT", FAKE_ARGV=str(self.argv), POWERSHELL_TELEMETRY_OPTOUT="1")
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        env.update(extra_env)
        return subprocess.run(
            [self.shell, "-NoProfile", "-NonInteractive", "-File", str(self.script), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def handed_over(self) -> list[str]:
        return self.argv.read_text(encoding="utf-8").splitlines()


@posix_only
@pytest.mark.parametrize("shell", shell_params)
class TestSimulatedRun:
    @pytest.mark.parametrize(
        ("shim", "args", "expected"),
        [
            (DEPLOY, [], ["rotate-cert"]),
            (DEPLOY, ["-Yes", "-Verbose"], ["rotate-cert", "--yes", "--verbose"]),
            (TEARDOWN, [], ["teardown"]),
            (TEARDOWN, ["-Force"], ["teardown", "--yes"]),
            (
                TEARDOWN,
                ["-KeepSetupCredential", "-Yes"],
                ["teardown", "--keep-setup-credential", "--yes"],
            ),
            (
                TEARDOWN,
                ["-DeleteAgent", "-PurgeData", "-Yes"],
                ["teardown", "--delete-agent", "--purge-data", "--yes"],
            ),
            (STATUS, [], ["status"]),
            (
                STATUS,
                ["-Json", "-HealthOnly", "-Strict"],
                ["status", "--json", "--health-only", "--strict"],
            ),
        ],
        ids=[
            "deploy",
            "deploy-yes-verbose",
            "teardown",
            "teardown-force",
            "teardown-keep-setup-credential",
            "teardown-everything",
            "status",
            "status-all-flags",
        ],
    )
    def test_the_python_command_gets_exactly_these_arguments(
        self, shell: str, tmp_path: Path, shim: Path, args: list[str], expected: list[str]
    ) -> None:
        folder = _Folder(tmp_path, shell, shim)

        result = folder.run(*args)

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        command, *flags = expected
        assert folder.handed_over() == [
            "PYTHONUTF8=1",
            "PYTHONIOENCODING=utf-8",
            "-m",
            "entrabot.winsetup",
            command,
            "--project-root",
            str(folder.root),
            *flags,
        ]
        assert "from the python command" in result.stdout  # its output reaches the person

    @pytest.mark.parametrize("shim", SHIMS, ids=lambda p: p.name)
    @pytest.mark.parametrize("code", [1, 3, 130])
    def test_the_exit_code_is_the_python_commands(
        self, shell: str, tmp_path: Path, shim: Path, code: int
    ) -> None:
        folder = _Folder(tmp_path, shell, shim)

        assert folder.run(FAKE_EXIT=str(code)).returncode == code

    @pytest.mark.parametrize("shim", SHIMS, ids=lambda p: p.name)
    def test_without_a_venv_it_says_to_run_setup_and_exits_1(
        self, shell: str, tmp_path: Path, shim: Path
    ) -> None:
        folder = _Folder(tmp_path, shell, shim, installed=False)

        result = folder.run()

        assert result.returncode == 1
        assert "setup-windows.cmd" in result.stdout + result.stderr
        assert not folder.argv.exists()

    def test_status_without_a_venv_keeps_stdout_empty(self, shell: str, tmp_path: Path) -> None:
        folder = _Folder(tmp_path, shell, STATUS, installed=False)

        result = folder.run("-Json")

        assert result.stdout == ""  # a -Json caller never reads an error as output
        assert "setup-windows.cmd" in result.stderr

    @pytest.mark.parametrize("shim", SHIMS, ids=lambda p: p.name)
    def test_off_windows_it_refuses(self, shell: str, tmp_path: Path, shim: Path) -> None:
        folder = _Folder(tmp_path, shell, shim)

        result = folder.run(OS="")

        assert result.returncode == 1
        assert "must run on Windows" in result.stdout + result.stderr
        assert not folder.argv.exists()

    def test_deploy_status_delegates_to_the_status_wrapper(
        self, shell: str, tmp_path: Path
    ) -> None:
        folder = _Folder(tmp_path, shell, DEPLOY)

        result = folder.run("-Status", "-Json", FAKE_EXIT="1")

        assert result.returncode == 1
        assert folder.handed_over()[4:] == ["status", "--project-root", str(folder.root), "--json"]

    @pytest.mark.parametrize("shim", SHIMS, ids=lambda p: p.name)
    def test_help_runs_nothing(self, shell: str, tmp_path: Path, shim: Path) -> None:
        folder = _Folder(tmp_path, shell, shim)

        result = folder.run("-Help")

        assert result.returncode == 0
        assert not folder.argv.exists()

    @pytest.mark.parametrize("shim", SHIMS, ids=lambda p: p.name)
    def test_a_stray_argument_is_an_error_not_a_guess(
        self, shell: str, tmp_path: Path, shim: Path
    ) -> None:
        folder = _Folder(tmp_path, shell, shim)

        result = folder.run("everything")  # a bare word is not a parameter of any of them

        assert result.returncode != 0
        assert not folder.argv.exists()


# ─────────────────────────────────────────────────────────────────────────────
# CI: the Windows job checks these files where a customer's machine would trip on them
# ─────────────────────────────────────────────────────────────────────────────

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-windows.yml"


def _ascii_step_files() -> list[str]:
    """The file list of the workflow's ASCII-only step."""
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text[text.index("ASCII-only") :]
    assignment = re.search(r"\$files = (.+)", step)
    assert assignment is not None
    return re.findall(r"'([^']+)'", assignment.group(1))


class TestWindowsCi:
    @pytest.mark.parametrize(
        "path",
        [
            "scripts/deploy-windows.ps1",
            "scripts/teardown-windows.ps1",
            "status-windows.ps1",
            "start-entrabot.cmd",
        ],
    )
    def test_every_windows_entry_point_is_in_the_ascii_only_check(self, path: str) -> None:
        # Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page, and cmd.exe
        # reads a batch file in the OEM one: a single non-ASCII character breaks either.
        assert path in _ascii_step_files()

    def test_the_setup_entry_points_are_still_checked(self) -> None:
        files = _ascii_step_files()
        for path in ("scripts/setup-windows.ps1", "scripts/prereqs-windows.ps1"):
            assert path in files

    def test_every_checked_file_exists(self) -> None:
        for path in _ascii_step_files():
            assert (REPO_ROOT / path).is_file(), path
