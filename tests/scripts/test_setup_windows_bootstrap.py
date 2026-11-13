"""Tests for the Windows setup bootstrap: setup-windows.cmd / setup-windows.ps1 / prereqs.

``scripts/setup-windows.ps1`` is now a BOOTSTRAP ONLY. A stock Windows machine has
Windows PowerShell 5.1 and nothing else, so the bootstrap must run there, needs no
elevation, finds or installs Python, builds ``.venv`` and hands off to the Python wizard
(``python -m entrabot.winsetup``), which owns everything else.

Two kinds of test, following the repo convention for ``.ps1`` files:

* static text assertions, which run everywhere;
* executed tests that dot-source the script and call its pure functions. They run under
  every PowerShell that is available: ``pwsh`` when it is on PATH (or named by
  ``ENTRABOT_TEST_PWSH``) and, on Windows, the in-box ``powershell.exe`` 5.1 as well.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._powershell import shell_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_PS1 = REPO_ROOT / "scripts" / "setup-windows.ps1"
SETUP_CMD = REPO_ROOT / "scripts" / "setup-windows.cmd"
PREREQS_PS1 = REPO_ROOT / "scripts" / "prereqs-windows.ps1"

PS51_ONLY_SCRIPTS = [SETUP_PS1, PREREQS_PS1]

# The SHA-512 of each official Python 3.12.10 package on nuget.org, base64, which is the
# form NuGet publishes (PackageHash, PackageHashAlgorithm SHA512). Cross-checked
# 2026-11-11: each package downloaded from https://www.nuget.org/api/v2/package/<id>/3.12.10
# and hashed locally, against the PackageHash in
# https://www.nuget.org/api/v2/Packages(Id='<id>',Version='3.12.10'). Both agreed.
PINNED_PYTHON_NUPKG_SHA512 = {
    "python": (
        "u9pNz2iKlCEbYtUJaKkbOPMF0LjR7NkCafdKhvigpPzrt8oWKgdTpHaR6z3wyWQAm9PYGUxv0Zr66NX9AeHMDw=="
    ),
    "pythonarm64": (
        "s3YP8TRRHOfg/JPmc96KP1bLPIlOjgt+MsJLyocX+BJ0qy9RmwHwogDZLnp9LsArUCmpJPd1LP+aUIXbX2kW2w=="
    ),
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _code(path: Path) -> str:
    """The script with comments and the leading help block removed."""
    text = _read(path)
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _cmd_code() -> str:
    """setup-windows.cmd without its ``rem`` lines."""
    lines = _read(SETUP_CMD).splitlines()
    return "\n".join(line for line in lines if not line.lstrip().lower().startswith("rem"))


def _function(code: str, name: str) -> str:
    """The text of one top-level PowerShell function, up to the next one."""
    start = code.index(f"function {name} ")
    following = code.find("\nfunction ", start + 1)
    return code[start:] if following == -1 else code[start:following]


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


def _ps(shell: str, script: Path, expression: str) -> object:
    """Dot-source ``script`` and return ``expression`` decoded from JSON."""
    command = (
        "$ErrorActionPreference = 'Stop'; . $env:EB_SCRIPT; "
        f"ConvertTo-Json -Compress -Depth 6 -InputObject ({expression})"
    )
    env = shell_env(shell)
    env["EB_SCRIPT"] = str(script)
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


# ─────────────────────────────────────────────────────────────────────────────
# Static: Windows PowerShell 5.1 compatibility
# ─────────────────────────────────────────────────────────────────────────────


class TestRunsOnWindowsPowerShell51:
    @pytest.mark.parametrize("path", PS51_ONLY_SCRIPTS, ids=lambda p: p.name)
    def test_ascii_only_and_no_bom(self, path: Path) -> None:
        # 5.1 reads a BOM-less file as the ANSI code page, so a single non-ASCII
        # character (a box-drawing rule, a check mark) is a parse error there.
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        raw.decode("ascii")

    @pytest.mark.parametrize("path", PS51_ONLY_SCRIPTS, ids=lambda p: p.name)
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
            "-o tsv",  # AGENTS.md: parse az as JSON
            "--output tsv",
            "utf8NoBOM",  # 7-only encoding name
            "-AsHashtable",  # 7-only ConvertFrom-Json switch
            "#Requires -Version 7",
        ],
    )
    def test_no_powershell7_only_or_forbidden_construct(self, path: Path, construct: str) -> None:
        assert construct not in _code(path)

    @pytest.mark.parametrize("path", PS51_ONLY_SCRIPTS, ids=lambda p: p.name)
    def test_no_ternary(self, path: Path) -> None:
        assert re.search(r"\)\s\?\s.+\s:\s", _code(path)) is None

    @pytest.mark.parametrize("path", PS51_ONLY_SCRIPTS, ids=lambda p: p.name)
    @pytest.mark.parametrize("name", ["args", "input", "matches", "host", "pid", "home", "error"])
    def test_no_assignment_to_automatic_variables(self, path: Path, name: str) -> None:
        assert re.search(rf"(?i)\${name}\s*\+?=(?!=)", _code(path)) is None

    @pytest.mark.parametrize("path", PS51_ONLY_SCRIPTS, ids=lambda p: p.name)
    def test_progress_records_are_silenced_before_anything_runs(self, path: Path) -> None:
        # 5.1 writes CLIXML progress ("Preparing modules for first use") to stderr when
        # its output is not a console, and its progress bar makes downloads crawl.
        code = _code(path)
        assert code.index("$ProgressPreference = 'SilentlyContinue'") < code.index("function ")

    def test_progress_is_silenced_in_the_cmd_preflight_and_the_smoke_wrapper(self) -> None:
        assert "$ProgressPreference = 'SilentlyContinue'" in _read(SETUP_CMD)
        smoke = _read(REPO_ROOT / "tests" / "smoke" / "smokeit.ps1")
        assert "$ProgressPreference = 'SilentlyContinue'" in smoke

    def test_setup_does_not_need_pwsh7_or_the_old_prereq_tools(self) -> None:
        code = _code(SETUP_PS1)
        assert "pwsh" not in code
        # Work IQ is opt-in: the bootstrap only forwards the flag, it never probes the CLI.
        without_flag_names = (
            code.lower()
            .replace("configurea365workiq", "")
            .replace("--configure-a365-work-iq", "")
            .replace("a365agentname", "")
        )
        assert "a365" not in without_flag_names
        assert "Get-Tpm" not in code
        assert "az account" not in code
        assert "icacls" not in code


# ─────────────────────────────────────────────────────────────────────────────
# Static: the .cmd entry point
# ─────────────────────────────────────────────────────────────────────────────


class TestCmdLauncher:
    def test_uses_inbox_windows_powershell_with_bypass(self) -> None:
        cmd = _read(SETUP_CMD)
        assert r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" in cmd
        assert "-NoProfile -ExecutionPolicy Bypass -File" in cmd
        assert "%~dp0setup-windows.ps1" in cmd
        assert "%*" in cmd
        assert "pwsh" not in cmd.lower()

    def test_propagates_the_exit_code(self) -> None:
        cmd = _read(SETUP_CMD)
        assert re.search(r"(?im)^\s*exit /b %EB_RC%\s*$", cmd)

    def test_pauses_only_for_a_double_click(self) -> None:
        cmd = _read(SETUP_CMD)
        assert "pause" in cmd.lower()
        # A window opened just to run this file was started as `cmd.exe /c "<file>"`.
        assert "!CMDCMDLINE!" in cmd
        assert "/c" in cmd
        # Never pause an unattended or remote run, or inside a terminal that stays open.
        assert "-Yes" in cmd
        assert "SSH_CONNECTION" in cmd
        assert "WT_SESSION" in cmd

    def test_double_click_detection_needs_no_wmi(self) -> None:
        # Measured on Windows 11: a standard user in a remote session gets "Access
        # denied" from Win32_Process, and that red error was the first thing on screen.
        cmd = _read(SETUP_CMD)
        assert "Get-CimInstance" not in cmd
        assert "Win32_Process" not in cmd
        assert "wmic" not in cmd.lower()
        assert "Get-WmiObject" not in cmd

    def test_delayed_expansion_is_scoped_to_the_detection_only(self) -> None:
        # Delayed expansion eats "!" characters, so it must be off again before the
        # caller's arguments are passed on with %*.
        cmd = _read(SETUP_CMD)
        on = cmd.index("setlocal EnableDelayedExpansion")
        off = cmd.index("endlocal & set")
        launch = cmd.index("-NoProfile -ExecutionPolicy Bypass -File")
        assert on < off < launch

    def test_explains_a_group_policy_block_in_plain_words(self) -> None:
        cmd = _read(SETUP_CMD)
        assert "MachinePolicy" in cmd
        assert "UserPolicy" in cmd
        assert "Group Policy" in cmd
        assert "IT" in cmd

    def test_has_no_labels_or_goto(self) -> None:
        # A .cmd checked out with LF endings mis-parses labels; none are needed here.
        cmd = _read(SETUP_CMD)
        assert re.search(r"(?im)^\s*:[A-Za-z]", cmd) is None
        assert "goto" not in cmd.lower()
        assert "call :" not in cmd.lower()

    def test_is_ascii(self) -> None:
        SETUP_CMD.read_bytes().decode("ascii")

    def test_never_interpolates_the_script_path_into_powershell_source(self) -> None:
        # An apostrophe in the profile path (O'Brien) must not break the launcher.
        cmd = _read(SETUP_CMD)
        assert "$env:EB_SETUP_PS1" in cmd
        assert "'%~dp0" not in cmd

    def test_caller_arguments_are_expanded_only_on_the_powershell_command_line(self) -> None:
        # `set "EB_ARGS= %* "` turned the caller's quoting inside out: with
        # -Subscription "R&D Sandbox" the "&" landed outside quotes and cmd ran
        # `D Sandbox" "` as a command. %* is now expanded exactly once, unquoted, on the
        # line that starts PowerShell, so it keeps the quoting the caller gave it.
        code = _cmd_code()
        with_args = [line for line in code.splitlines() if "%*" in line]
        assert with_args == [
            '"%EB_PS%" -NoProfile -ExecutionPolicy Bypass -File "%EB_SETUP_PS1%" %*'
        ]
        # No single arguments either (%1, %~1, ...): each has the same hazard.
        assert re.search(r"%(~[a-z]*)?[1-9]", code) is None
        assert "EB_ARGS" not in code

    def test_yes_is_read_from_the_command_line_never_from_the_arguments(self) -> None:
        # !CMDCMDLINE! is read with delayed expansion, which never re-parses the value.
        # It is only consulted when cmd.exe was started for THIS file (/c ... <name>),
        # and then this file's arguments are part of it, so -Yes is visible there.
        code = _cmd_code()
        assert 'set "EB_LINE=!CMDCMDLINE!"' in code
        assert 'if "!EB_LINE:%~nx0=!"=="!EB_LINE!" set "EB_PAUSE=0"' in code
        assert 'if not "!EB_LINE: -Yes=!"=="!EB_LINE!" set "EB_PAUSE=0"' in code
        # Every pause decision happens while delayed expansion is on, before it is off.
        on = code.index("setlocal EnableDelayedExpansion")
        off = code.index("endlocal & set")
        for needle in ("!EB_LINE: /c =!", "!EB_LINE:%~nx0=!", "!EB_LINE: -Yes=!"):
            assert on < code.index(needle) < off

    def test_has_no_parenthesised_blocks(self) -> None:
        # Like labels, a multi-line ( ... ) block mis-parses when the file has LF endings.
        for line in _cmd_code().splitlines():
            assert not line.rstrip().endswith("("), line
            assert not line.lstrip().startswith(")"), line

    def test_windows_powershell_does_not_inherit_powershell_7_module_paths(self) -> None:
        # about_PSModulePath, "Starting Windows PowerShell from PowerShell 7": a
        # powershell.exe started through cmd.exe keeps PowerShell 7's module paths and
        # can fail to load its own in-box modules; clearing the variable makes it
        # rebuild its default.
        code = _cmd_code()
        clear = code.index('set "PSModulePath="')
        assert code.index("setlocal") < clear < code.index('"%EB_PS%"')


# ─────────────────────────────────────────────────────────────────────────────
# Static: the bootstrap contract
# ─────────────────────────────────────────────────────────────────────────────


class TestBootstrapContract:
    @pytest.mark.parametrize(
        "declaration",
        [
            "[switch]$NewChain",
            "[string]$UseBlueprint",
            "[string]$UpnSuffix",
            "[string]$AgentUserUpn",
            "[string[]]$Sponsor",
            "[switch]$UseCloudMemory",
            "[switch]$KeepMemoryLocal",
            "[string]$WithStorageAccount",
            "[string]$WithContainer",
            "[switch]$CreateNewStorage",
            "[string]$Location",
            "[string]$Subscription",
            "[switch]$ConfigureA365WorkIq",
            "[string]$A365AgentName",
            "[string]$Tenant",
            "[switch]$DeviceCode",
            "[string]$Hosts",
            "[switch]$SkipSmoke",
            "[switch]$Yes",
            "[switch]$Diagnose",
            "[switch]$BootstrapOnly",
            "[switch]$Migrate",
            "[switch]$Status",
            "[switch]$Json",
            "[switch]$HealthOnly",
            "[switch]$Strict",
            "[switch]$Help",
        ],
    )
    def test_parameter_is_declared(self, declaration: str) -> None:
        assert declaration in _read(SETUP_PS1)

    def test_status_delegates_to_the_status_wrapper_unchanged(self) -> None:
        script = _read(SETUP_PS1)
        assert "status-windows.ps1" in script
        assert "-Json:$Json -HealthOnly:$HealthOnly -Strict:$Strict" in script

    def test_hands_off_to_the_python_wizard_in_utf8_mode(self) -> None:
        script = _read(SETUP_PS1)
        assert "$env:PYTHONUTF8 = '1'" in script
        assert "$env:PYTHONIOENCODING = 'utf-8'" in script
        assert "'-m', 'entrabot.winsetup'" in script
        handoff = script.index("'-m', 'entrabot.winsetup'")
        assert script.index("$env:PYTHONUTF8 = '1'") < handoff
        assert "exit $LASTEXITCODE" in script[handoff:]

    def test_installs_runtime_and_provisioning_deps_but_not_dev(self) -> None:
        code = _code(SETUP_PS1)
        assert "'--prefer-binary'" in code
        # the setup scripts import azure-identity + requests, which only that extra brings
        assert "$installExtras = 'provisioning'" in code
        assert "'-e', ($ProjectRoot + '[' + $installExtras + ']')" in code
        assert "[dev]" not in code
        assert "--quiet" not in code  # a multi-minute silent step looks hung
        assert "'--log'" in code  # pip writes the full log itself; nothing is redirected

    def test_never_installs_build_tools_itself(self) -> None:
        code = _code(SETUP_PS1)
        assert "VisualStudio" not in code
        assert "prereqs-windows.ps1 -WithBuildTools" in code

    def test_python_install_is_per_user_and_verified(self) -> None:
        code = _code(SETUP_PS1)
        for arg in (
            "'Python.Python.3.12'",
            "'--scope', 'user'",
            "'--accept-package-agreements'",
            "'--accept-source-agreements'",
            "'--disable-interactivity'",
        ):
            assert arg in code
        installer = _function(code, "Install-PythonFromPythonOrg")
        assert "Python Software Foundation" in code
        for arg in ("'/quiet'", "'InstallAllUsers=0'", "'PrependPath=0'", "'Include_launcher=0'"):
            assert arg in installer
        # Verification comes before the installer is ever started.
        assert installer.index("Get-AuthenticodeSignature") < installer.index("Start-Process")

    def test_msi_unavailable_is_not_fatal(self) -> None:
        # Measured on Windows 11: the python.org installer returns 1601 ("Windows
        # Installer service could not be accessed") for a non-admin in an SSH session.
        code = _code(SETUP_PS1)
        assert "1601" in code
        assert "1603" in code
        installer = _function(code, "Install-PythonFromPythonOrg")
        assert "Test-InstallerUnavailable" in installer
        assert "Stop-Setup" not in installer

    def test_private_python_is_verified_before_it_is_ever_run_or_kept(self) -> None:
        code = _code(SETUP_PS1)
        private = _function(code, "Get-PrivatePython")
        assert "Invoke-WebRequest" in private
        assert "Expand-Archive" in private
        # Trust is checked on the staged copy, before it is moved into place...
        staged_check = private.index("Test-PrivatePythonTrusted -ToolsDir $stagedTools")
        assert private.index("Expand-Archive") < staged_check < private.index("Rename-Item")
        # ...again every time an existing copy is reused, and nothing in here runs it.
        assert "Test-PrivatePythonTrusted -ToolsDir $toolsDir" in private
        assert "& $" not in private
        trusted = _function(code, "Test-PrivatePythonTrusted")
        assert "Get-AuthenticodeSignature" in trusted
        assert "Test-TrustedPythonSignature" in trusted
        assert "python3*.dll" in trusted

    def test_the_whole_private_python_package_is_pinned_before_it_is_unpacked(self) -> None:
        # Authenticode covers python.exe and python3*.dll only; vcruntime140*.dll,
        # DLLs\*.pyd and Lib\ are unsigned. The SHA-512 of the .nupkg covers all of it.
        code = _code(SETUP_PS1)
        private = _function(code, "Get-PrivatePython")
        download = private.index("Invoke-WebRequest")
        hashed = private.index("Get-FileHash -LiteralPath $package -Algorithm SHA512")
        compared = private.index("Test-PackageHashMatch")
        unpacked = private.index("Expand-Archive")
        assert download < hashed < compared < unpacked
        # A package with no pinned hash is never downloaded at all.
        assert private.index("Get-PythonNuGetSha512") < download
        # A mismatch deletes the download and falls through (return $null) to the next route.
        mismatch = private[compared:unpacked]
        assert "Remove-Item -LiteralPath $package" in mismatch
        assert "return $null" in mismatch
        assert "Stop-Setup" not in private

    def test_on_screen_message_says_what_was_actually_verified(self) -> None:
        code = _code(SETUP_PS1)
        private = _function(code, "Get-PrivatePython")
        assert "Package signature verified" not in code
        assert "SHA-512" in private
        assert "python.exe and python3*.dll" in private

    def test_python_routes_are_tried_in_the_agreed_order(self) -> None:
        code = _code(SETUP_PS1)
        main = code[code.index("$bound = @{}") :]
        order = [
            main.index("Find-Python"),
            main.index("Get-PrivatePython"),
            main.index("Install-PythonWithWinget"),
            main.index("Install-PythonFromPythonOrg"),
            main.index("python-31210"),  # plain instructions, last
        ]
        assert order == sorted(order)

    def test_user_is_told_which_python_route_was_used(self) -> None:
        code = _code(SETUP_PS1)
        assert "already on this PC" in code
        assert "private copy of Python" in code
        assert "nothing else on this PC is changed" in code

    def test_old_data_is_settled_before_anything_is_written_under_the_data_folder(self) -> None:
        # The private Python lives in %LOCALAPPDATA%\entrabot. Writing there first would
        # make an un-migrated install look like "two data folders" to the migration.
        code = _code(SETUP_PS1)
        main = code[code.index("$bound = @{}") :]
        assert main.index("Move-LegacyData") < main.index("Get-PrivatePython")

    def test_session_owner_lookup_fails_silently(self) -> None:
        # WMI can refuse a standard or remote user. That must mean "cannot tell, do not
        # block", with nothing printed, never a red PowerShell error.
        owner = _function(_code(SETUP_PS1), "Get-SessionOwnerSid")
        assert owner.count("-ErrorAction Stop") >= 2  # both CIM calls are catchable
        catch = owner[owner.index("} catch {") :]
        assert "Write-" not in catch
        assert "return ''" in catch

    def test_pip_output_is_captured_not_streamed(self) -> None:
        # ~90 lines of "Collecting/Downloading" drowned the bootstrap. pip now runs with
        # its output in files and a dot every couple of seconds as the sign of life.
        code = _code(SETUP_PS1)
        assert "Invoke-QuietNative -Exe $venvPython -Arguments $pipArgs" in code
        # The raw, streaming form exists once, and only for -Verbose.
        assert code.count("& $venvPython @pipArgs") == 1
        assert code.index("if ($verboseRun) {") < code.index("& $venvPython @pipArgs")
        runner = _function(code, "Invoke-QuietNative")
        assert "-RedirectStandardOutput" in runner
        assert "-RedirectStandardError" in runner
        assert "Write-Host '.' -NoNewline" in runner
        assert "$process.Handle" in runner  # without this 5.1 loses the exit code
        main = code[code.index("$bound = @{}") :]
        assert "Get-Content -LiteralPath" in main  # last lines are shown on failure
        assert "-Tail 25" in main or "Select-Object -Last 25" in main

    def test_diagnose_never_builds_or_changes_anything(self) -> None:
        code = _code(SETUP_PS1)
        main = code[code.index("$bound = @{}") :]
        assert "$diagnoseOnly" in main
        # Every modifying step is behind a not-diagnose branch.
        assert "-Diagnose only inspects an existing setup" in main
        guard = main.index("if ($diagnoseOnly) {")
        assert guard < main.index("Stop-ProjectMcpServer -VenvDir $venvDir")
        assert guard < main.index("Invoke-QuietNative -Exe $venvPython -Arguments $pipArgs")

    def test_install_is_skipped_when_the_stamp_matches(self) -> None:
        code = _code(SETUP_PS1)
        assert "entrabot-install.stamp" in code
        assert "Get-FileHash" in code
        assert "pyproject.toml" in code
        assert "Entrabot is already installed here" in code
        main = code[code.index("$bound = @{}") :]
        # The stamp is only written after pip succeeded.
        assert main.index("Stop-Setup \"Installing Entrabot's packages failed") < main.index(
            "Set-Content -LiteralPath $stampPath"
        )

    def test_winget_is_judged_by_running_it_not_by_presence(self) -> None:
        # On a profile that has never logged on interactively winget is not registered;
        # a present-but-broken winget must fall through to the python.org download.
        code = _code(SETUP_PS1)
        probe = code.index("'--version'")
        assert code.index("function Test-WingetWorks") < probe
        assert "Get-PythonInstallerUrl" in code
        assert "Invoke-WebRequest" in code
        assert "Tls12" in code  # 5.1 on older builds defaults to TLS 1.0/1.1

    def test_long_paths_are_warned_about_never_changed(self) -> None:
        code = _code(SETUP_PS1)
        assert "LongPathsEnabled" in code
        assert "Set-ItemProperty" not in code
        assert "New-ItemProperty" not in code

    def test_never_tells_the_user_to_reopen_the_terminal(self) -> None:
        text = _read(SETUP_PS1).lower()
        assert "reopen" not in text
        assert "new terminal" not in text
        assert "restart your terminal" not in text

    def test_main_body_is_guarded_for_dot_sourcing(self) -> None:
        script = _read(SETUP_PS1)
        guard = script.index("$MyInvocation.InvocationName -eq '.'")
        assert guard < script.index("'-m', 'entrabot.winsetup'")
        # every function the tests call is defined before the guard
        for name in ("Get-WizardArguments", "Select-PreferredPython", "Test-StoreAliasPath"):
            assert script.index(f"function {name}") < guard

    def test_legacy_migration_helper_has_no_double_quotes(self) -> None:
        # 5.1 strips embedded double quotes from native arguments, which would turn the
        # helper into a Python syntax error.
        script = _read(SETUP_PS1)
        match = re.search(r"\$MigrationCode = '([^']|'')*'", script)
        assert match is not None
        assert "migrate_legacy_data_dir" in match.group(0)
        assert '"' not in match.group(0)


# ─────────────────────────────────────────────────────────────────────────────
# Static: prereqs is optional now
# ─────────────────────────────────────────────────────────────────────────────


class TestPrereqsIsOptional:
    @pytest.mark.parametrize(
        "switch", ["$WithWorkIq", "$WithBuildTools", "$WithClaudeCode", "$WithGit"]
    )
    def test_heavy_extras_are_opt_in_switches(self, switch: str) -> None:
        assert f"[switch]{switch}" in _read(PREREQS_PS1)

    def test_says_setup_installs_what_it_needs(self) -> None:
        assert "setup-windows.cmd" in _read(PREREQS_PS1)

    def test_nothing_is_installed_without_a_switch(self) -> None:
        code = _code(PREREQS_PS1)
        # No unconditional installs of the things setup now handles itself.
        assert "Python.Python" not in code
        assert "Microsoft.AzureCLI" not in code
        # PowerShell 7 only for Work IQ, whose Agent 365 CLI needs it for one of its steps.
        work_iq = code[code.index("if ($WithWorkIq) {") : code.index("if ($WithBuildTools) {")]
        assert code.count("'Microsoft.PowerShell'") == work_iq.count("'Microsoft.PowerShell'") == 1

    def test_claude_code_uses_the_documented_native_installer(self) -> None:
        code = _code(PREREQS_PS1)
        assert "irm https://claude.ai/install.ps1 | iex" in code
        # Run in its own PowerShell, so this script's strict mode cannot break the
        # vendor's installer; and judged by running the result, not by the exit code alone.
        claude = _function(code, "Install-ClaudeCode")
        assert "'-ExecutionPolicy', 'Bypass'" in claude
        assert "Invoke-Expression" not in code
        assert "Get-VersionLine" in _function(code, "Find-ClaudeCode")
        assert "'--version'" in _function(code, "Get-VersionLine")

    def test_claude_code_installer_shell_rebuilds_its_own_module_path(self) -> None:
        # This script can itself be a Windows PowerShell that inherited PowerShell 7's
        # PSModulePath (started through cmd.exe or Python from a pwsh terminal); 5.1 keeps
        # those paths and passes them on. The child gets no PSModulePath, so it builds
        # its default, and this process gets its own value back afterwards.
        claude = _function(_code(PREREQS_PS1), "Install-ClaudeCode")
        saved = claude.index("$savedModulePath = $env:PSModulePath")
        cleared = claude.index("$env:PSModulePath = $null")
        started = claude.index("& $shell @installArgs")
        restored = claude.index("$env:PSModulePath = $savedModulePath")
        assert saved < cleared < started < restored
        assert claude.index("finally") < restored

    def test_git_needs_neither_winget_nor_admin(self) -> None:
        # The wizard calls -WithGit for a standard user in a session where winget does not
        # exist (it is absent until the first interactive logon).
        code = _code(PREREQS_PS1)
        assert "Git.Git" not in code
        git_block = code[code.index("if ($WithGit) {") : code.index("if ($WithClaudeCode) {")]
        assert "winget" not in git_block.lower()
        assert "Install-GitWithInstaller" in git_block
        assert "Install-PortableGit" in git_block
        assert git_block.index("Install-GitWithInstaller") < git_block.index("Install-PortableGit")
        installer = _function(code, "Install-GitWithInstaller")
        assert "/VERYSILENT" in installer
        assert "/NORESTART" in installer
        portable = _function(code, "Install-PortableGit")
        assert "'-o\"'" in portable  # 7-Zip style: -o"<folder>" -y
        assert "-y" in portable

    def test_git_downloads_are_authenticode_verified_before_they_run(self) -> None:
        code = _code(PREREQS_PS1)
        assert "Johannes Schindelin" in code  # the Git for Windows release signer
        save = _function(code, "Save-VerifiedDownload")
        assert save.index("Invoke-WebRequest") < save.index("Get-AuthenticodeSignature")
        assert "Test-TrustedPublisher" in save
        for name in ("Install-GitWithInstaller", "Install-PortableGit"):
            body = _function(code, name)
            assert body.index("Save-VerifiedDownload") < body.index("Start-Process")

    def test_git_bash_is_never_taken_from_path(self) -> None:
        # `bash` on PATH can be C:\Windows\System32\bash.exe, the WSL launcher.
        code = _code(PREREQS_PS1)
        assert "Get-Command bash" not in code
        assert "CLAUDE_CODE_GIT_BASH_PATH" in code
        assert "Get-VersionLine" in _function(code, "Find-GitBash")

    def test_paths_on_a_missing_drive_cannot_crash_the_lookups(self) -> None:
        # Join-Path / Split-Path throw "Cannot find drive" for a drive that is not there
        # (found by running these helpers on a machine with no C: drive at all).
        prereqs = _code(PREREQS_PS1)
        for name in ("Get-GitBashCandidates", "Test-GitBashNeedsOverride"):
            body = _function(prereqs, name)
            assert "Join-Path" not in body
            assert "Split-Path" not in body
        candidates = _function(_code(SETUP_PS1), "Get-PythonCandidatePaths")
        assert "Join-Path ([string]$props" not in candidates

    def test_claude_folder_goes_on_the_user_path_only(self) -> None:
        # The Claude Code installer warns that %USERPROFILE%\.local\bin is not on PATH.
        code = _code(PREREQS_PS1)
        add = _function(code, "Add-UserPathEntry")
        assert "[Microsoft.Win32.Registry]::CurrentUser" in add
        assert "DoNotExpandEnvironmentNames" in add  # %VARS% in existing entries survive
        assert "GetValueKind" in add  # REG_EXPAND_SZ stays REG_EXPAND_SZ
        assert "Get-UserPathWithEntry" in add
        # The machine PATH is read (to refresh this window) but never written.
        assert "LocalMachine" not in code
        assert re.search(r"SetEnvironmentVariable\([^)]*'Machine'", code) is None
        claude_block = code[code.index("if ($WithClaudeCode) {") : code.index("if ($WithWorkIq) {")]
        assert "Add-UserPathEntry" in claude_block
        assert "New terminal windows will find" in claude_block

    def test_each_tool_must_be_proven_usable_to_count_as_installed(self) -> None:
        code = _code(PREREQS_PS1)
        git_block = code[code.index("if ($WithGit) {") : code.index("if ($WithClaudeCode) {")]
        claude_block = code[code.index("if ($WithClaudeCode) {") : code.index("if ($WithWorkIq) {")]
        work_iq = code[code.index("if ($WithWorkIq) {") : code.index("if ($WithBuildTools) {")]
        # After every install route the tool is looked up (and run) again.
        assert git_block.count("Find-GitBash") >= 2
        assert claude_block.count("Find-ClaudeCode") >= 2
        assert work_iq.count("Find-A365") >= 2 and work_iq.count("Get-DotnetSdk") >= 2
        assert "$failed += 'Git for Windows'" in git_block
        assert "$failed += 'Claude Code'" in claude_block
        assert "'--version'" in _function(code, "Get-VersionLine")
        assert "Get-VersionLine" in _function(code, "Find-A365")
        assert "'--list-sdks'" in _function(code, "Get-DotnetSdk")

    def test_work_iq_needs_neither_winget_nor_admin_for_dotnet_and_a365(self) -> None:
        # From a real run as a standard user over SSH: `winget install Microsoft.DotNet.SDK.9`
        # exited 1459 (it needs elevation and a desktop), so Work IQ stayed off.
        code = _code(PREREQS_PS1)
        assert "Microsoft.DotNet.SDK" not in code
        assert "$DotnetInstallUrl = 'https://dot.net/v1/dotnet-install.ps1'" in code
        assert "$MicrosoftPublisher = 'Microsoft Corporation'" in code
        install = _function(code, "Install-PrivateDotnet")
        # Microsoft's script is checked (Authenticode, signed by Microsoft) before it runs,
        # and runs in its own in-box Windows PowerShell with nothing put on PATH.
        assert install.index("Save-VerifiedDownload") < install.index("& $shell @installArgs")
        assert "-Organization $MicrosoftPublisher" in install
        for argument in ("'-Channel', $DotnetChannel", "'-InstallDir', $Folder", "'-NoPath'"):
            assert argument in install
        assert "System32\\WindowsPowerShell\\v1.0\\powershell.exe" in install
        assert "$env:PSModulePath = $null" in install
        a365 = _function(code, "Install-A365")
        a365 = a365[: a365.index("\nif ($Help) {")]  # the last function before the main body
        assert "'--tool-path', $Folder" in a365 and "--global" not in a365
        assert "'--allow-roll-forward'" in a365  # a .NET 10 SDK runs a365 whatever it targets
        assert "entrabot\\tools" in _function(code, "Get-WorkIqToolFolders")

    def test_dotnet_is_the_long_term_support_release(self) -> None:
        # .NET 8 and 9 reached end of support on 10 November 2026 (Microsoft Learn, "Releases
        # and support for .NET"); the Agent 365 CLI needs .NET 8 or later.
        code = _code(PREREQS_PS1)
        assert "$DotnetChannel = '10.0'" in code
        assert "$DotnetMinimumMajor = 8" in code

    def test_entrabots_own_dotnet_is_named_for_a365_and_sends_no_telemetry(self) -> None:
        use = _function(_code(PREREQS_PS1), "Use-DotnetSdk")
        assert "$env:DOTNET_ROOT = $Sdk.Root" in use
        for wins in ("DOTNET_ROOT_X64", "DOTNET_ROOT_X86", "DOTNET_ROOT_ARM64", "DOTNET_ROOT(x86)"):
            assert wins in use  # cleared: each would win over DOTNET_ROOT
        assert "$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'" in use
        find = _function(_code(PREREQS_PS1), "Find-A365")
        saved = find.index("$savedRoot = $env:DOTNET_ROOT")
        assert saved < find.index("$env:DOTNET_ROOT = $savedRoot")


# ─────────────────────────────────────────────────────────────────────────────
# Executed: parse + pure functions, under every available PowerShell
# ─────────────────────────────────────────────────────────────────────────────

ROOT = r"C:\Users\O'Brien\My Stuff\entrabot"


def _args(shell: str, bound: str) -> list[str]:
    expression = f"@(Get-WizardArguments -Bound {bound} -ProjectRoot $env:EB_ROOT)"
    result = _ps(shell, SETUP_PS1, expression)
    assert isinstance(result, list)
    return [str(item) for item in result]


@pytest.fixture(autouse=True)
def _root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EB_ROOT", ROOT)


@pytest.mark.parametrize("shell", shell_params)
class TestParses:
    @pytest.mark.parametrize("path", PS51_ONLY_SCRIPTS, ids=lambda p: p.name)
    def test_script_parses_without_errors(self, shell: str, path: Path) -> None:
        command = (
            "$errors = $null; $tokens = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$env:EB_SCRIPT, [ref]$tokens, [ref]$errors); "
            "ConvertTo-Json -Compress -InputObject @($errors | ForEach-Object { $_.ToString() })"
        )
        env = shell_env(shell)
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


@pytest.mark.parametrize("shell", shell_params)
class TestWizardArguments:
    def test_no_parameters_runs_the_interactive_wizard(self, shell: str) -> None:
        assert _args(shell, "@{}") == ["setup", "--project-root", ROOT]

    def test_new_chain_with_suffix_unattended(self, shell: str) -> None:
        bound = "@{ NewChain = $true; UpnSuffix = 'bob'; Yes = $true }"
        assert _args(shell, bound) == [
            "setup", "--project-root", ROOT, "--new-chain", "--upn-suffix", "bob", "--yes",
        ]  # fmt: skip

    def test_every_setup_parameter_maps_to_its_flag(self, shell: str) -> None:
        bound = (
            "@{ UseBlueprint = '11111111-2222-3333-4444-555555555555';"
            " AgentUserUpn = 'agent@contoso.com';"
            " Sponsor = @('a@contoso.com,b@contoso.com', 'c@contoso.com');"
            " UseCloudMemory = $true; WithStorageAccount = 'stg'; WithContainer = 'mem';"
            " CreateNewStorage = $true; Location = 'westus2'; Subscription = 'My Sub';"
            " ConfigureA365WorkIq = $true; Tenant = 'contoso.com'; DeviceCode = $true;"
            " Hosts = 'claude'; SkipSmoke = $true }"
        )
        assert _args(shell, bound) == [
            "setup", "--project-root", ROOT,
            "--use-blueprint", "11111111-2222-3333-4444-555555555555",
            "--agent-user-upn", "agent@contoso.com",
            "--sponsor", "a@contoso.com",
            "--sponsor", "b@contoso.com",
            "--sponsor", "c@contoso.com",
            "--memory", "cloud",
            "--storage-account", "stg",
            "--container", "mem",
            "--create-new-storage",
            "--location", "westus2",
            "--subscription", "My Sub",
            "--configure-a365-work-iq",
            "--tenant", "contoso.com",
            "--device-code",
            "--hosts", "claude",
            "--skip-smoke",
        ]  # fmt: skip

    def test_keep_memory_local_maps_to_memory_local(self, shell: str) -> None:
        assert _args(shell, "@{ KeepMemoryLocal = $true }")[3:] == ["--memory", "local"]

    def test_a_switch_passed_as_false_is_not_forwarded(self, shell: str) -> None:
        bound = "@{ NewChain = $false; UseCloudMemory = $false; Yes = $false; UpnSuffix = '' }"
        assert _args(shell, bound) == ["setup", "--project-root", ROOT]

    def test_deprecated_parameters_are_accepted_and_dropped(self, shell: str) -> None:
        bound = "@{ A365AgentName = 'EntraBot Code Agent'; Migrate = $true }"
        assert _args(shell, bound) == ["setup", "--project-root", ROOT]

    def test_verbose_maps_to_the_wizards_verbose_flag(self, shell: str) -> None:
        bound = "@{ NewChain = $true; Yes = $true; Verbose = $true }"
        assert _args(shell, bound) == [
            "setup", "--project-root", ROOT, "--new-chain", "--yes", "--verbose",
        ]  # fmt: skip
        assert "--verbose" not in _args(shell, "@{ Verbose = $false }")

    def test_install_stamp(self, shell: str) -> None:
        def ps(expr: str) -> object:
            result = _ps(shell, SETUP_PS1, f"@{{ v = ({expr}) }}")
            assert isinstance(result, dict)
            return result["v"]

        assert ps("Get-InstallStamp -PyprojectHash 'ABCDEF' -Extras 'provisioning'") == (
            "abcdef|provisioning"
        )
        current = "Test-InstallCurrent -Expected 'abc|provisioning' -Recorded"
        assert ps(f"{current} 'abc|provisioning'") is True
        assert ps(f"{current} ' abc|provisioning '") is True  # stray newline or spaces
        assert ps(f"{current} 'abd|provisioning'") is False  # pyproject.toml changed
        assert ps(f"{current} 'abc|dev'") is False  # different extras
        assert ps(f"{current} ''") is False  # never installed
        assert ps("Test-InstallCurrent -Expected '' -Recorded ''") is False

    def test_diagnose_selects_the_diagnose_subcommand(self, shell: str) -> None:
        bound = "@{ Diagnose = $true; NewChain = $true; Yes = $true }"
        assert _args(shell, bound) == ["diagnose", "--project-root", ROOT]

    def test_conflicting_parameters_are_reported_before_anything_is_installed(
        self, shell: str
    ) -> None:
        def problems(bound: str) -> list[str]:
            result = _ps(shell, SETUP_PS1, f"@(Get-ParameterProblems -Bound {bound})")
            assert isinstance(result, list)
            return [str(item) for item in result]

        assert problems("@{ NewChain = $true }") == []
        assert "-NewChain" in problems("@{ NewChain = $true; UseBlueprint = 'x' }")[0]
        both_memory_modes = "@{ UseCloudMemory = $true; KeepMemoryLocal = $true }"
        assert "-UseCloudMemory" in problems(both_memory_modes)[0]
        assert "-CreateNewStorage" in problems(
            "@{ CreateNewStorage = $true; WithStorageAccount = 'stg' }"
        )[0]


@pytest.mark.parametrize("shell", shell_params)
class TestPythonDiscovery:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("3.12.10", "3.12.10"),
            ("Python 3.13.1", "3.13.1"),
            ("  3.12.0\r\n", "3.12.0"),
            ("", None),
            ("Python was not found; run without arguments to install", None),
        ],
    )
    def test_version_parsing(self, shell: str, text: str, expected: str | None) -> None:
        os.environ["EB_TEXT"] = text
        try:
            result = _ps(
                shell,
                SETUP_PS1,
                "@{ v = [string](ConvertTo-PythonVersion -Text $env:EB_TEXT) }",
            )
        finally:
            del os.environ["EB_TEXT"]
        assert isinstance(result, dict)
        assert (result["v"] or None) == expected

    @pytest.mark.parametrize(
        ("text", "ok"),
        [("3.11.9", False), ("3.12.0", True), ("3.13.2", True), ("3.14.0", True), ("", False)],
    )
    def test_minimum_version_is_3_12(self, shell: str, text: str, ok: bool) -> None:
        expr = f"@{{ ok = (Test-PythonVersionOk -Text '{text}') }}"
        result = _ps(shell, SETUP_PS1, expr)
        assert isinstance(result, dict)
        assert result["ok"] is ok

    @pytest.mark.parametrize(
        ("path", "is_alias"),
        [
            (r"C:\Users\a\AppData\Local\Microsoft\WindowsApps\python.exe", True),
            (r"c:\users\a\appdata\local\microsoft\windowsapps\python3.exe", True),
            (
                r"C:\Users\a\AppData\Local\Microsoft\WindowsApps"
                r"\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe",
                True,
            ),
            (r"C:\Users\a\AppData\Local\Programs\Python\Python312\python.exe", False),
            (r"C:\Program Files\Python313\python.exe", False),
        ],
    )
    def test_store_alias_detection(self, shell: str, path: str, is_alias: bool) -> None:
        os.environ["EB_PATH_UNDER_TEST"] = path
        try:
            result = _ps(
                shell, SETUP_PS1, "@{ a = (Test-StoreAliasPath -Path $env:EB_PATH_UNDER_TEST) }"
            )
        finally:
            del os.environ["EB_PATH_UNDER_TEST"]
        assert isinstance(result, dict)
        assert result["a"] is is_alias

    def test_py_launcher_listing_is_parsed(self, shell: str) -> None:
        lines = (
            "@(' -V:3.13 *        "
            "C:\\Users\\a\\AppData\\Local\\Programs\\Python\\Python313\\python.exe',"
            " ' -V:3.12          C:\\Program Files\\Python312\\python.exe',"
            " 'Installed Pythons found by py Launcher for Windows',"
            " ' -3.11-64        C:\\x y\\python.exe', '')"
        )
        result = _ps(shell, SETUP_PS1, f"@(Get-PyLauncherPaths -Lines {lines})")
        assert result == [
            r"C:\Users\a\AppData\Local\Programs\Python\Python313\python.exe",
            r"C:\Program Files\Python312\python.exe",
            r"C:\x y\python.exe",
        ]

    def test_preferred_python_is_the_ci_tested_3_12_then_3_13_then_newest(self, shell: str) -> None:
        def pick(candidates: str) -> str | None:
            result = _ps(
                shell,
                SETUP_PS1,
                f"@{{ p = [string](Select-PreferredPython -Candidates {candidates}) }}",
            )
            assert isinstance(result, dict)
            return result["p"] or None

        c311 = "@{ Path = 'C:\\p311\\python.exe'; Version = '3.11.9' }"
        c312 = "@{ Path = 'C:\\p312\\python.exe'; Version = '3.12.10' }"
        c313 = "@{ Path = 'C:\\p313\\python.exe'; Version = '3.13.1' }"
        c314 = "@{ Path = 'C:\\p314\\python.exe'; Version = '3.14.0' }"
        c315 = "@{ Path = 'C:\\p315\\python.exe'; Version = '3.15.0' }"
        store = (
            "@{ Path = 'C:\\Users\\a\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe';"
            " Version = '3.12.10' }"
        )
        assert pick(f"@({c311}, {c313}, {c312}, {c314})") == r"C:\p312\python.exe"
        assert pick(f"@({c314}, {c313})") == r"C:\p313\python.exe"
        assert pick(f"@({c314}, {c315}, {c311})") == r"C:\p315\python.exe"
        assert pick(f"@({c311})") is None
        assert pick(f"@({store})") is None
        assert pick("@()") is None

    @pytest.mark.parametrize(
        ("arch", "expected"),
        [
            ("amd64", "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"),
            ("arm64", "https://www.python.org/ftp/python/3.12.10/python-3.12.10-arm64.exe"),
            ("x86", None),
        ],
    )
    def test_installer_url_per_architecture(
        self, shell: str, arch: str, expected: str | None
    ) -> None:
        result = _ps(
            shell, SETUP_PS1, f"@{{ u = [string](Get-PythonInstallerUrl -Architecture '{arch}') }}"
        )
        assert isinstance(result, dict)
        assert (result["u"] or None) == expected

    @pytest.mark.parametrize(
        ("arch", "expected"),
        [
            ("amd64", "https://www.nuget.org/api/v2/package/python/3.12.10"),
            ("arm64", "https://www.nuget.org/api/v2/package/pythonarm64/3.12.10"),
            ("x86", None),
        ],
    )
    def test_private_python_package_per_architecture(
        self, shell: str, arch: str, expected: str | None
    ) -> None:
        result = _ps(
            shell, SETUP_PS1, f"@{{ u = [string](Get-PythonNuGetUrl -Architecture '{arch}') }}"
        )
        assert isinstance(result, dict)
        assert (result["u"] or None) == expected

    @pytest.mark.parametrize(
        ("code", "unavailable"), [(1601, True), (1603, True), (0, False), (1, False)]
    )
    def test_installer_unavailable_codes(self, shell: str, code: int, unavailable: bool) -> None:
        result = _ps(
            shell, SETUP_PS1, f"@{{ u = (Test-InstallerUnavailable -ExitCode {code}) }}"
        )
        assert isinstance(result, dict)
        assert result["u"] is unavailable

    @pytest.mark.parametrize(
        ("route", "fragment"),
        [
            ("existing", "Using Python 3.12.10 already on this PC"),
            ("private-new", "Downloaded a private copy of Python 3.12.10 for Entrabot"),
            ("private", "private copy of Python 3.12.10"),
            ("venv", "existing Python environment"),
        ],
    )
    def test_route_message(self, shell: str, route: str, fragment: str) -> None:
        expr = f"@{{ m = (Get-PythonRouteMessage -Route '{route}' -Version '3.12.10') }}"
        result = _ps(shell, SETUP_PS1, expr)
        assert isinstance(result, dict)
        assert fragment in result["m"]
        if route.startswith("private"):
            assert "nothing else on this PC is changed" in result["m"]

    @pytest.mark.parametrize(
        ("native", "wow64", "expected"),
        [
            ("AMD64", "", "amd64"),
            ("ARM64", "", "arm64"),
            ("x86", "AMD64", "amd64"),  # 32-bit shell on 64-bit Windows
            ("x86", "", "x86"),
        ],
    )
    def test_machine_architecture(
        self, shell: str, native: str, wow64: str, expected: str
    ) -> None:
        expr = (
            "@{ a = (Get-MachineArchitecture"
            f" -ProcessorArchitecture '{native}' -ProcessorArchiteW6432 '{wow64}') }}"
        )
        result = _ps(shell, SETUP_PS1, expr)
        assert isinstance(result, dict)
        assert result["a"] == expected

    @pytest.mark.parametrize(
        ("status", "subject", "trusted"),
        [
            (
                "Valid",
                "CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton,"
                " S=Oregon, C=US",
                True,
            ),
            ("NotSigned", "", False),
            ("HashMismatch", "CN=Python Software Foundation, O=Python Software Foundation", False),
            ("Valid", "CN=Evil Corp, O=Evil Corp, C=US", False),
            ("Valid", "CN=x, O=Python Software Foundation Fake, C=US", False),
            ("Valid", "CN=O=Python Software Foundation, O=Evil Corp", False),
            # A quoted CN may contain ", O=...," text; only the real O component counts.
            ('Valid', 'CN="Evil, O=Python Software Foundation, x", O=Evil Corp, C=US', False),
            ('Valid', 'CN="a,O=Python Software Foundation,b", O=Evil Corp', False),
            # An escaped comma is part of the value, not a separator.
            ("Valid", r"CN=x, O=Python Software Foundation\, Evil, C=US", False),
            # Two organizations is not "signed by the PSF".
            ("Valid", "CN=x, O=Python Software Foundation, O=Evil Corp", False),
            # Exact, case-sensitive value.
            ("Valid", "CN=x, O=python software foundation, C=US", False),
            # Unparseable subjects are untrusted.
            ('Valid', 'CN="unterminated, O=Python Software Foundation', False),
            # A quoted value is still that value.
            ('Valid', 'CN=Python Software Foundation, O="Python Software Foundation", C=US', True),
        ],
    )
    def test_installer_signature_must_be_valid_and_from_the_psf(
        self, shell: str, status: str, subject: str, trusted: bool
    ) -> None:
        os.environ["EB_SUBJECT"] = subject
        try:
            result = _ps(
                shell,
                SETUP_PS1,
                "@{ t = (Test-TrustedPythonSignature"
                f" -Status '{status}' -Subject $env:EB_SUBJECT) }}",
            )
        finally:
            del os.environ["EB_SUBJECT"]
        assert isinstance(result, dict)
        assert result["t"] is trusted

    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            (
                "CN=Python Software Foundation, O=Python Software Foundation, C=US",
                [["CN", "Python Software Foundation"], ["O", "Python Software Foundation"],
                 ["C", "US"]],
            ),
            ('CN="a, O=b", O=c', [["CN", "a, O=b"], ["O", "c"]]),
            ('CN="say ""hi""", O=c', [["CN", 'say "hi"'], ["O", "c"]]),
            (r"CN=a\, b, O=c", [["CN", "a, b"], ["O", "c"]]),
            ("CN=a + O=b; C=d", [["CN", "a"], ["O", "b"], ["C", "d"]]),
            ('CN="open', None),
            ('CN="a"x, O=b', None),
            ("no equals sign", None),
            ("", None),
        ],
    )  # fmt: skip
    def test_distinguished_name_parsing(
        self, shell: str, subject: str, expected: list[list[str]] | None
    ) -> None:
        os.environ["EB_SUBJECT"] = subject
        try:
            result = _ps(
                shell,
                SETUP_PS1,
                "@{ p = (ConvertFrom-DistinguishedName -Subject $env:EB_SUBJECT) }",
            )
        finally:
            del os.environ["EB_SUBJECT"]
        assert isinstance(result, dict)
        if expected is None:
            assert result["p"] is None
        else:
            assert [[part["Key"], part["Value"]] for part in result["p"]] == expected

    @pytest.mark.parametrize(
        ("arch", "expected"),
        [
            ("amd64", PINNED_PYTHON_NUPKG_SHA512["python"]),
            ("arm64", PINNED_PYTHON_NUPKG_SHA512["pythonarm64"]),
            ("x86", None),
        ],
    )
    def test_private_python_package_hash_is_pinned_per_architecture(
        self, shell: str, arch: str, expected: str | None
    ) -> None:
        result = _ps(
            shell, SETUP_PS1, f"@{{ h = [string](Get-PythonNuGetSha512 -Architecture '{arch}') }}"
        )
        assert isinstance(result, dict)
        assert (result["h"] or None) == expected

    def test_package_hash_comparison(self, shell: str) -> None:
        digest = hashlib.sha512(b"entrabot").digest()
        hex_upper = digest.hex().upper()
        flipped = ("0" if hex_upper[0] != "0" else "1") + hex_upper[1:]
        cases = {
            "upper": (hex_upper, True),
            "lower": (hex_upper.lower(), True),
            "flipped": (flipped, False),
            "short": (hex_upper[:-2], False),
            "nonhex": ("Z" * 128, False),
            "empty": ("", False),
        }
        os.environ["EB_EXPECTED"] = base64.b64encode(digest).decode("ascii")
        try:
            pairs = "; ".join(
                f"{name} = (Test-PackageHashMatch -Sha512Hex '{value}'"
                " -ExpectedBase64 $env:EB_EXPECTED)"
                for name, (value, _) in cases.items()
            )
            result = _ps(shell, SETUP_PS1, f"@{{ {pairs}; noexpected = (Test-PackageHashMatch"
                         f" -Sha512Hex '{hex_upper}' -ExpectedBase64 '') }}")  # fmt: skip
        finally:
            del os.environ["EB_EXPECTED"]
        assert isinstance(result, dict)
        for name, (_, matches) in cases.items():
            assert result[name] is matches, name
        assert result["noexpected"] is False

    def test_get_filehash_output_is_compared_in_the_published_form(
        self, shell: str, tmp_path: Path
    ) -> None:
        package = tmp_path / "package.nupkg"
        package.write_bytes(b"not really a package\n" * 100)
        expected = base64.b64encode(hashlib.sha512(package.read_bytes()).digest()).decode()
        os.environ["EB_FILE"] = str(package)
        os.environ["EB_EXPECTED"] = expected
        try:
            result = _ps(
                shell,
                SETUP_PS1,
                "@{ m = (Test-PackageHashMatch -Sha512Hex (Get-FileHash -LiteralPath"
                " $env:EB_FILE -Algorithm SHA512).Hash -ExpectedBase64 $env:EB_EXPECTED) }",
            )
        finally:
            del os.environ["EB_FILE"]
            del os.environ["EB_EXPECTED"]
        assert isinstance(result, dict)
        assert result["m"] is True



@pytest.mark.parametrize("shell", shell_params)
class TestPrivatePythonDownload:
    """``Get-PrivatePython`` with the network, the unpacker and Authenticode stubbed."""

    def _run(self, shell: str, tmp_path: Path, payload: bytes, pin: str | None) -> dict:
        temp = tmp_path / "temp"
        temp.mkdir()
        data = tmp_path / "data"
        stubs = """
$global:notes = @(); $global:oks = @(); $global:downloaded = $false
$global:expanded = $false; $global:authenticode = $false
function Write-Note { param([string]$Message) $global:notes += $Message }
function Write-Info { param([string]$Message) }
function Write-Ok { param([string]$Message) $global:oks += $Message }
function Enable-Tls12 { }
function Invoke-WebRequest {
    param([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing)
    $global:downloaded = $true
    [System.IO.File]::WriteAllBytes($OutFile, [System.Convert]::FromBase64String($env:EB_PAYLOAD))
}
function Expand-Archive {
    param([string]$LiteralPath, [string]$DestinationPath, [switch]$Force)
    $global:expanded = $true
    $tools = Join-Path $DestinationPath 'tools'
    New-Item -ItemType Directory -Path $tools -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $tools 'python.exe') -Value 'x'
}
function Test-PrivatePythonTrusted {
    param([string]$ToolsDir)
    $global:authenticode = $true
    return $true
}
"""
        # "real" keeps the pinned hash from the script; otherwise the pin is replaced.
        if pin != "real":
            stubs += (
                "function Get-PythonNuGetSha512 { param([string]$Architecture)"
                " if ($env:EB_PIN) { return $env:EB_PIN }; return $null }\n"
            )
        private = _function(_read(SETUP_PS1), "Get-PrivatePython")
        expression = (
            f"$(\n{private}\n{stubs}\n"
            "$r = Get-PrivatePython -DataRoot $env:EB_DATA\n"
            "@{ path = [string]$(if ($r) { $r.Path } else { '' });"
            " notes = @($global:notes); oks = @($global:oks);"
            " downloaded = $global:downloaded; expanded = $global:expanded;"
            " authenticode = $global:authenticode;"
            " leftovers = @(Get-ChildItem -LiteralPath $env:TEMP -Force).Count })"
        )
        env_update = {
            "EB_PAYLOAD": base64.b64encode(payload).decode("ascii"),
            "EB_PIN": pin or "",
            "EB_DATA": str(data),
            "TEMP": str(temp),
            "PROCESSOR_ARCHITECTURE": "AMD64",
        }
        saved = {name: os.environ.get(name) for name in [*env_update, "PROCESSOR_ARCHITEW6432"]}
        os.environ.update(env_update)
        os.environ.pop("PROCESSOR_ARCHITEW6432", None)
        try:
            result = _ps(shell, SETUP_PS1, expression)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        assert isinstance(result, dict)
        return result

    def test_a_package_that_does_not_match_the_pin_is_deleted_and_never_unpacked(
        self, shell: str, tmp_path: Path
    ) -> None:
        result = self._run(shell, tmp_path, b"a tampered package", pin="real")
        assert result["downloaded"] is True
        assert result["expanded"] is False
        assert result["authenticode"] is False
        assert result["path"] == ""  # falls through to the next route
        assert result["leftovers"] == 0  # the download is gone
        assert any("SHA-512" in note and "NOT used" in note for note in result["notes"])
        assert not any("Could not download" in note for note in result["notes"])

    def test_a_matching_package_is_unpacked_then_signature_checked_and_kept(
        self, shell: str, tmp_path: Path
    ) -> None:
        payload = b"the genuine package"
        pin = base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")
        result = self._run(shell, tmp_path, payload, pin=pin)
        assert result["expanded"] is True
        assert result["authenticode"] is True
        assert result["path"].endswith("python.exe")
        assert result["leftovers"] == 0
        assert result["notes"] == []
        assert any("SHA-512" in ok for ok in result["oks"])
        assert any("python.exe and python3*.dll" in ok for ok in result["oks"])

    def test_no_pin_means_no_download(self, shell: str, tmp_path: Path) -> None:
        result = self._run(shell, tmp_path, b"anything", pin=None)
        assert result["downloaded"] is False
        assert result["path"] == ""


@pytest.mark.parametrize("shell", shell_params)
class TestNativeArgumentQuoting:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("install", "install"),
            ("--prefer-binary", "--prefer-binary"),
            (r"C:\src\entrabot[provisioning]", r"C:\src\entrabot[provisioning]"),
            (
                r"C:\Users\O'Brien\My Stuff\entrabot",
                '"' + r"C:\Users\O'Brien\My Stuff\entrabot" + '"',
            ),
            ("C:\\dir with space\\", '"C:\\dir with space\\\\"'),  # trailing backslash doubles
            ('say "hi"', r'"say \"hi\""'),
            ("", '""'),
        ],
    )
    def test_one_argument(self, shell: str, value: str, expected: str) -> None:
        os.environ["EB_VALUE"] = value
        try:
            result = _ps(
                shell, SETUP_PS1, "@{ q = (ConvertTo-NativeArgument -Value $env:EB_VALUE) }"
            )
        finally:
            del os.environ["EB_VALUE"]
        assert isinstance(result, dict)
        assert result["q"] == expected

    def test_argument_line(self, shell: str) -> None:
        expr = (
            "@{ line = (Join-NativeArguments -Arguments"
            " @('-m', 'pip', 'install', '-e', 'C:\\a b\\entrabot[provisioning]')) }"
        )
        result = _ps(shell, SETUP_PS1, expr)
        assert isinstance(result, dict)
        assert result["line"] == '-m pip install -e "C:\\a b\\entrabot[provisioning]"'


@pytest.mark.parametrize("shell", shell_params)
class TestGuardsAndDiagnostics:
    @pytest.mark.parametrize(
        ("current", "owner", "elevated", "different"),
        [
            # Elevated with someone else's (admin) credentials on the user's desktop.
            ("S-1-5-21-1-1001", "S-1-5-21-1-1002", True, True),
            # Same person, elevated: fine (the stores are the same user's).
            ("S-1-5-21-1-1001", "S-1-5-21-1-1001", True, False),
            # Nobody at the console to compare with (plain SSH / remote session).
            ("S-1-5-21-1-1001", "", True, False),
            ("S-1-5-21-1-1001", "", False, False),
            # A different, NON-elevated identity is a normal second logon, not our case.
            ("S-1-5-21-1-1001", "S-1-5-21-1-1002", False, False),
            ("", "S-1-5-21-1-1002", True, False),
        ],
    )
    def test_different_account_detection(
        self, shell: str, current: str, owner: str, elevated: bool, different: bool
    ) -> None:
        flag = "$true" if elevated else "$false"
        expr = (
            f"@{{ d = (Test-DifferentAccount -CurrentSid '{current}'"
            f" -SessionOwnerSid '{owner}' -Elevated {flag}) }}"
        )
        result = _ps(shell, SETUP_PS1, expr)
        assert isinstance(result, dict)
        assert result["d"] is different

    @pytest.mark.parametrize(
        ("root", "enabled", "risky"),
        [
            ("C:\\src\\entrabot", 0, False),
            ("C:\\Users\\someone\\" + "d" * 90 + "\\entrabot", 0, True),
            ("C:\\Users\\someone\\" + "d" * 90 + "\\entrabot", 1, False),
        ],
    )
    def test_long_path_risk_is_flagged_only_when_long_paths_are_off(
        self, shell: str, root: str, enabled: int, risky: bool
    ) -> None:
        expr = (
            f"@{{ r = (Test-LongPathRisk -ProjectRoot '{root}'"
            f" -LongPathsEnabled {enabled}) }}"
        )
        result = _ps(shell, SETUP_PS1, expr)
        assert isinstance(result, dict)
        assert result["r"] is risky

    @pytest.mark.parametrize(
        ("legacy", "current", "action"),
        [
            (False, False, "none"),
            (False, True, "none"),
            (True, False, "move"),
            (True, True, "conflict"),
        ],
    )
    def test_legacy_data_action(
        self, shell: str, legacy: bool, current: bool, action: str
    ) -> None:
        expr = (
            "@{ a = (Get-LegacyDataAction"
            f" -LegacyHasContent ${str(legacy).lower()}"
            f" -CurrentHasContent ${str(current).lower()}) }}"
        )
        result = _ps(shell, SETUP_PS1, expr)
        assert isinstance(result, dict)
        assert result["a"] == action

    def test_compiler_needed_is_recognised_in_pip_output(self, shell: str) -> None:
        def needed(lines: str) -> bool:
            result = _ps(shell, SETUP_PS1, f"@{{ n = (Test-CompilerNeeded -Lines {lines}) }}")
            assert isinstance(result, dict)
            return bool(result["n"])

        assert needed(
            "@('  error: Microsoft Visual C++ 14.0 or greater is required."
            " Get it with ''Microsoft C++ Build Tools''')"
        )
        assert needed("@('error: command ''cl.exe'' failed: None')")
        assert not needed("@('Successfully installed entrabot-identity-research-0.1')")
        assert not needed("@()")

    @pytest.mark.parametrize(
        ("code", "ok"),
        [
            (0, True),
            (-1978335189, True),  # no applicable update
            (-1978335135, True),  # package already installed
            (-1978335153, True),  # upgrade version not newer
            (-1978334963, True),  # another version already installed
            (1, False),
            (-1978335216, False),  # no applicable installer
        ],
    )
    def test_winget_already_installed_codes_count_as_success(
        self, shell: str, code: int, ok: bool
    ) -> None:
        result = _ps(shell, PREREQS_PS1, f"@{{ ok = (Test-WingetSuccess -ExitCode ({code})) }}")
        assert isinstance(result, dict)
        assert result["ok"] is ok


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe runs only on Windows")

# Stands in for setup-windows.ps1: shows exactly which arguments PowerShell received.
_ARG_ECHO_PS1 = """[CmdletBinding(PositionalBinding = $false)]
param([string]$Subscription = '', [string]$UpnSuffix = '', [switch]$Yes)
Write-Output ('SUBSCRIPTION=[' + $Subscription + ']')
Write-Output ('UPNSUFFIX=[' + $UpnSuffix + ']')
Write-Output ('YES=' + [bool]$Yes)
$inbox = Get-Command Get-AuthenticodeSignature -ErrorAction SilentlyContinue
Write-Output ('INBOX_MODULES=' + [bool]$inbox)
exit 7
"""


@windows_only
@pytest.mark.parametrize("newline", ["\r\n", "\n"], ids=["crlf", "lf"])
class TestCmdLauncherOnWindows:
    """setup-windows.cmd run by the real cmd.exe (CRLF and LF checkouts)."""

    def _launch(
        self, tmp_path: Path, newline: str, arguments: str
    ) -> subprocess.CompletedProcess[str]:
        folder = tmp_path / "R&D it's here" / "scripts"
        folder.mkdir(parents=True)
        launcher = folder / "setup-windows.cmd"
        text = _read(SETUP_CMD).replace("\r\n", "\n")
        launcher.write_bytes(text.replace("\n", newline).encode("ascii"))
        (folder / "setup-windows.ps1").write_text(_ARG_ECHO_PS1, encoding="ascii")
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in {"WT_SESSION", "SSH_CONNECTION", "SSH_CLIENT"}
        }
        env["PSModulePath"] = r"C:\does-not-exist\PowerShell\7\Modules"
        # A string command line is passed to CreateProcess verbatim: this is what
        # Explorer and other shells do, `cmd.exe /c ""<file>" <arguments>"`.
        return subprocess.run(
            f'cmd.exe /d /c ""{launcher}" {arguments}"',
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def test_quoted_ampersand_and_bang_reach_powershell_unchanged(
        self, tmp_path: Path, newline: str
    ) -> None:
        result = self._launch(
            tmp_path, newline, '-Subscription "R&D Sandbox" -UpnSuffix "wow!" -Yes'
        )
        output = result.stdout + result.stderr
        assert result.returncode == 7, output
        assert "SUBSCRIPTION=[R&D Sandbox]" in result.stdout
        assert "UPNSUFFIX=[wow!]" in result.stdout
        assert "YES=True" in result.stdout
        # PSModulePath pointed at a PowerShell 7 folder; 5.1 still finds its own modules.
        assert "INBOX_MODULES=True" in result.stdout
        assert "is not recognized" not in output  # nothing was run as a command
        assert "Press any key" not in output  # -Yes never pauses

    def test_a_window_opened_for_this_file_pauses_without_yes(
        self, tmp_path: Path, newline: str
    ) -> None:
        result = self._launch(tmp_path, newline, '-Subscription "R&D Sandbox"')
        assert result.returncode == 7, result.stdout + result.stderr
        assert "SUBSCRIPTION=[R&D Sandbox]" in result.stdout
        assert "Press any key" in result.stdout


@posix_only
@pytest.mark.parametrize("shell", shell_params)
class TestRefusesOffWindows:
    def test_refuses_to_run_on_a_non_windows_host(self, shell: str) -> None:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-File", str(SETUP_PS1), "-Yes"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 1
        assert "must run on Windows" in result.stdout + result.stderr
        assert not (REPO_ROOT / ".venv" / "Scripts").exists()

    def test_conflicting_parameters_exit_2(self, shell: str) -> None:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-File", str(SETUP_PS1),
             "-NewChain", "-UseBlueprint", "x"],  # fmt: skip
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 2
        assert "-NewChain" in result.stdout + result.stderr


# ─────────────────────────────────────────────────────────────────────────────
# Executed: the whole main body, with the machine-specific helpers stubbed
# ─────────────────────────────────────────────────────────────────────────────

# Injected just before the main body. Everything else in the script runs for real.
_SIMULATION_STUBS = """function Test-Elevated { return $false }
function Get-LongPathsEnabled { return 1 }
function Find-Python { return $env:FAKE_PYTHON }
function Stop-ProjectMcpServer { param([string]$VenvDir) }
"""

# Stands in for python.exe: creates the venv layout, answers the version probe and the
# migration helper, behaves like a chatty pip, and echoes what the wizard was given.
_FAKE_PYTHON = r"""#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/Scripts" && cp "$0" "$3/Scripts/python.exe" && chmod +x "$3/Scripts/python.exe"
  exit 0
fi
if [ "$1" = "-c" ]; then
  case "$2" in
    *version_info*) echo "3.12.10"; exit 0 ;;
    *migrate_legacy_data_dir*) echo "migrated=False"; exit 0 ;;
    *entrabot.winsetup*) exit "${FAKE_IMPORT_EXIT:-0}" ;;
  esac
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  n=0
  while [ "$n" -lt 90 ]; do n=$((n + 1)); echo "Collecting fake-package-$n"; done
  printf '%s\n' "$@" > "$FAKE_PIP_ARGV"
  previous=""
  for argument in "$@"; do
    if [ "$previous" = "--log" ]; then echo "full pip log" > "$argument"; fi
    previous="$argument"
  done
  sleep "${FAKE_PIP_SECONDS:-0}"
  if [ -n "${FAKE_PIP_FAIL:-}" ]; then
    echo "error: Microsoft Visual C++ 14.0 or greater is required." >&2
    exit 1
  fi
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "entrabot.winsetup" ]; then
  shift 2
  printf 'WIZARD_ARG=%s\n' "$@"
  echo "UTF8=$PYTHONUTF8/$PYTHONIOENCODING"
  exit "${FAKE_WIZARD_EXIT:-0}"
fi
echo "fake python: unexpected arguments: $*" >&2
exit 99
"""


class _Simulation:
    def __init__(self, tmp_path: Path, shell: str) -> None:
        self.shell = shell
        # A space and an apostrophe: the two characters that used to break setup.
        self.root = tmp_path / "it's here" / "entrabot"
        (self.root / "scripts").mkdir(parents=True)
        self.pyproject = self.root / "pyproject.toml"
        self.pyproject.write_text('[project]\nname = "sim"\n', encoding="utf-8")
        source = SETUP_PS1.read_text(encoding="ascii")
        marker = "$bound = @{}\n"
        assert source.count(marker) == 1
        self.script = self.root / "scripts" / "setup-windows.ps1"
        self.script.write_text(source.replace(marker, _SIMULATION_STUBS + marker), encoding="ascii")
        self.fake_python = tmp_path / "bin" / "fakepython"
        self.fake_python.parent.mkdir()
        self.fake_python.write_text(_FAKE_PYTHON, encoding="ascii")
        self.fake_python.chmod(0o755)
        self.home = tmp_path / "home"
        self.local = self.home / "AppData" / "Local"
        self.local.mkdir(parents=True)
        self.temp = tmp_path / "temp"
        self.temp.mkdir()
        self.pip_argv = tmp_path / "pip-argv.txt"

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = shell_env(self.shell)
        env.update(
            OS="Windows_NT",
            LOCALAPPDATA=str(self.local),
            USERPROFILE=str(self.home),
            TEMP=str(self.temp),
            FAKE_PYTHON=str(self.fake_python),
            FAKE_PIP_ARGV=str(self.pip_argv),
            POWERSHELL_TELEMETRY_OPTOUT="1",
            NO_COLOR="1",
        )
        env.update(extra_env)
        return subprocess.run(
            [self.shell, "-NoProfile", "-NonInteractive", "-File", str(self.script), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )


@posix_only
@pytest.mark.parametrize("shell", shell_params)
class TestSimulatedRun:
    def test_success_path_is_calm_and_writes_nothing_to_the_error_stream(
        self, shell: str, tmp_path: Path
    ) -> None:
        sim = _Simulation(tmp_path, shell)
        result = sim.run("-BootstrapOnly", FAKE_PIP_SECONDS="2.5")

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        # One status line with dots as the sign of life; none of pip's 90 lines.
        status_line = r"Installing Entrabot and its packages \(a minute or two\) \.+"
        assert re.search(status_line, result.stdout)
        assert "Collecting fake-package" not in result.stdout
        assert "[ok] Entrabot installed" in result.stdout
        assert "[ok] Using Python 3.12.10 already on this PC" in result.stdout
        assert len(result.stdout.splitlines()) < 30
        # pip received the project path as ONE argument despite the space and apostrophe.
        argv = sim.pip_argv.read_text(encoding="utf-8").splitlines()
        assert argv[:3] == ["-m", "pip", "install"]
        assert argv[-2:] == ["-e", f"{sim.root}[provisioning]"]
        # The capture files are gone and the full log sits with the other logs.
        assert list(sim.temp.glob("*.out.txt")) == []
        assert list(sim.temp.glob("*.err.txt")) == []
        assert list(sim.temp.glob("*.log")) == []
        kept = list((sim.local / "entrabot" / "logs").glob("entrabot-bootstrap-pip-*.log"))
        assert len(kept) == 1
        assert kept[0].read_text(encoding="utf-8").strip() == "full pip log"

    def test_rerun_does_not_reinstall(self, shell: str, tmp_path: Path) -> None:
        sim = _Simulation(tmp_path, shell)
        assert sim.run("-BootstrapOnly").returncode == 0
        assert (sim.root / ".venv" / "entrabot-install.stamp").is_file()
        sim.pip_argv.unlink()

        again = sim.run("-BootstrapOnly")
        assert again.returncode == 0, again.stdout + again.stderr
        assert again.stderr == ""
        assert "existing Python environment (Python 3.12.10)" in again.stdout
        assert "Entrabot is already installed here" in again.stdout
        assert "Installing Entrabot and its packages" not in again.stdout
        assert not sim.pip_argv.exists()  # pip never ran

    def test_a_changed_project_definition_reinstalls(self, shell: str, tmp_path: Path) -> None:
        sim = _Simulation(tmp_path, shell)
        assert sim.run("-BootstrapOnly").returncode == 0
        sim.pip_argv.unlink()
        sim.pyproject.write_text('[project]\nname = "sim"\nversion = "2"\n', encoding="utf-8")

        again = sim.run("-BootstrapOnly")
        assert again.returncode == 0, again.stdout + again.stderr
        assert "Installing Entrabot and its packages" in again.stdout
        assert sim.pip_argv.exists()

    def test_a_broken_install_is_repaired_even_when_the_stamp_matches(
        self, shell: str, tmp_path: Path
    ) -> None:
        sim = _Simulation(tmp_path, shell)
        assert sim.run("-BootstrapOnly").returncode == 0
        sim.pip_argv.unlink()
        again = sim.run("-BootstrapOnly", FAKE_IMPORT_EXIT="1")
        assert again.returncode == 0, again.stdout + again.stderr
        assert again.stderr == ""
        assert sim.pip_argv.exists()

    def test_diagnose_on_a_folder_that_was_never_set_up_changes_nothing(
        self, shell: str, tmp_path: Path
    ) -> None:
        sim = _Simulation(tmp_path, shell)
        result = sim.run("-Diagnose")
        assert result.returncode == 1
        assert result.stderr == ""
        assert "not set up in this folder yet" in result.stdout
        assert "setup-windows.cmd" in result.stdout
        assert not (sim.root / ".venv").exists()
        assert not sim.pip_argv.exists()
        assert not (sim.local / "entrabot").exists()
        assert "WIZARD_ARG" not in result.stdout

    def test_diagnose_on_an_installed_folder_goes_straight_to_the_wizard(
        self, shell: str, tmp_path: Path
    ) -> None:
        sim = _Simulation(tmp_path, shell)
        assert sim.run("-BootstrapOnly").returncode == 0
        sim.pip_argv.unlink()
        # Even with a stale stamp, -Diagnose must not reinstall.
        sim.pyproject.write_text('[project]\nname = "changed"\n', encoding="utf-8")

        result = sim.run("-Diagnose", FAKE_WIZARD_EXIT="4")
        assert result.returncode == 4, result.stdout + result.stderr
        assert result.stderr == ""
        assert "WIZARD_ARG=diagnose" in result.stdout
        assert "Installing Entrabot and its packages" not in result.stdout
        assert not sim.pip_argv.exists()

    def test_diagnose_reports_a_broken_install_instead_of_fixing_it(
        self, shell: str, tmp_path: Path
    ) -> None:
        sim = _Simulation(tmp_path, shell)
        assert sim.run("-BootstrapOnly").returncode == 0
        sim.pip_argv.unlink()
        result = sim.run("-Diagnose", FAKE_IMPORT_EXIT="1")
        assert result.returncode == 1
        assert result.stderr == ""
        assert "not fully installed" in result.stdout
        assert not sim.pip_argv.exists()
        assert "WIZARD_ARG" not in result.stdout

    def test_verbose_shows_raw_pip_output_and_tells_the_wizard(
        self, shell: str, tmp_path: Path
    ) -> None:
        sim = _Simulation(tmp_path, shell)
        result = sim.run("-NewChain", "-Yes", "-Verbose")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Collecting fake-package-1\n" in result.stdout
        assert "Collecting fake-package-90\n" in result.stdout
        assert "WIZARD_ARG=--verbose" in result.stdout

    def test_hands_off_to_the_wizard_and_returns_its_exit_code(
        self, shell: str, tmp_path: Path
    ) -> None:
        sim = _Simulation(tmp_path, shell)
        result = sim.run(
            "-NewChain", "-UpnSuffix", "bob", "-Sponsor", "a@x.com,b@x.com", "-Yes",
            FAKE_WIZARD_EXIT="3",
        )  # fmt: skip
        assert result.returncode == 3, result.stdout + result.stderr
        assert result.stderr == ""
        wizard = [
            line[len("WIZARD_ARG=") :]
            for line in result.stdout.splitlines()
            if line.startswith("WIZARD_ARG=")
        ]
        assert wizard == [
            "setup", "--project-root", str(sim.root), "--new-chain", "--upn-suffix", "bob",
            "--sponsor", "a@x.com", "--sponsor", "b@x.com", "--yes",
        ]  # fmt: skip
        assert "UTF8=1/utf-8" in result.stdout

    def test_pip_failure_shows_the_last_lines_the_log_and_the_fix(
        self, shell: str, tmp_path: Path
    ) -> None:
        sim = _Simulation(tmp_path, shell)
        result = sim.run("-Yes", FAKE_PIP_FAIL="1")
        assert result.returncode == 1
        assert result.stderr == ""  # the failure is explained, not dumped as a PowerShell error
        assert "error: Microsoft Visual C++ 14.0 or greater is required." in result.stdout
        assert "Collecting fake-package-90" in result.stdout  # inside the last 25 lines
        assert "Collecting fake-package-10\n" not in result.stdout  # outside them
        assert "Full log:" in result.stdout
        assert "prereqs-windows.ps1 -WithBuildTools" in result.stdout
        assert "WIZARD_ARG" not in result.stdout


# The signer of the Git for Windows 2.56.1 release downloads, read from the files.
_GIT_SIGNER = "CN=Johannes Schindelin, O=Johannes Schindelin, L=Bruehl, C=DE"


@pytest.mark.parametrize("shell", shell_params)
class TestPrereqsHelpers:
    def _ps(self, shell: str, expr: str) -> object:
        result = _ps(shell, PREREQS_PS1, f"@{{ v = ({expr}) }}")
        assert isinstance(result, dict)
        return result["v"]

    @pytest.mark.parametrize(
        ("status", "subject", "trusted"),
        [
            ("Valid", _GIT_SIGNER, True),
            ("NotSigned", "", False),
            ("HashMismatch", _GIT_SIGNER, False),
            ("Valid", "CN=Someone Else, O=Someone Else, C=US", False),
            ("Valid", "CN=O=Johannes Schindelin, O=Evil Corp", False),
            ("Valid", "CN=x, O=Johannes Schindelin Ltd, C=US", False),
            # The old substring match accepted ", O=Johannes Schindelin," inside a quoted CN.
            ('Valid', 'CN="Evil, O=Johannes Schindelin, x", O=Evil Corp, C=US', False),
            ("Valid", r"CN=x, O=Johannes Schindelin\, Evil, C=US", False),
            ("Valid", "CN=x, O=Johannes Schindelin, O=Evil Corp", False),
            ('Valid', 'CN="unterminated, O=Johannes Schindelin', False),
        ],
    )
    def test_git_publisher_check(
        self, shell: str, status: str, subject: str, trusted: bool
    ) -> None:
        os.environ["EB_SUBJECT"] = subject
        try:
            value = self._ps(
                shell,
                f"Test-TrustedPublisher -Status '{status}' -Subject $env:EB_SUBJECT"
                " -Organization 'Johannes Schindelin'",
            )
        finally:
            del os.environ["EB_SUBJECT"]
        assert value is trusted

    @pytest.mark.parametrize(
        ("kind", "arch", "expected"),
        [
            (
                "installer",
                "amd64",
                "https://github.com/git-for-windows/git/releases/download/"
                "v2.56.1.windows.1/Git-2.56.1-64-bit.exe",
            ),
            (
                "installer",
                "arm64",
                "https://github.com/git-for-windows/git/releases/download/"
                "v2.56.1.windows.1/Git-2.56.1-arm64.exe",
            ),
            (
                "portable",
                "amd64",
                "https://github.com/git-for-windows/git/releases/download/"
                "v2.56.1.windows.1/PortableGit-2.56.1-64-bit.7z.exe",
            ),
            ("installer", "x86", None),
        ],
    )
    def test_pinned_git_download(
        self, shell: str, kind: str, arch: str, expected: str | None
    ) -> None:
        value = self._ps(
            shell, f"[string](Get-PinnedGitUrl -Kind '{kind}' -Architecture '{arch}')"
        )
        assert (value or None) == expected

    def test_latest_release_asset_is_selected_by_exact_name(self, shell: str) -> None:
        base = "https://github.com/git-for-windows/git/releases/download/v2.60.0.windows.1"
        names = [
            "Git-2.60.0-64-bit.exe",
            "Git-2.60.0-arm64.exe",
            "Git-2.60.0-64-bit.tar.bz2",
            "MinGit-2.60.0-64-bit.zip",
            "PortableGit-2.60.0-64-bit.7z.exe",
            "PortableGit-2.60.0-arm64.7z.exe",
        ]
        assets = ", ".join(
            f"@{{ name = '{name}'; browser_download_url = '{base}/{name}' }}" for name in names
        )
        elsewhere = (
            "@{ name = 'Git-9.9.9-64-bit.exe';"
            " browser_download_url = 'https://example.com/Git-9.9.9-64-bit.exe' }"
        )

        def pick(kind: str, arch: str, listing: str = assets) -> str | None:
            value = self._ps(
                shell,
                f"[string](Select-GitAssetUrl -Assets @({listing}) -Kind '{kind}'"
                f" -Architecture '{arch}')",
            )
            return str(value) if value else None

        assert pick("installer", "amd64") == f"{base}/Git-2.60.0-64-bit.exe"
        assert pick("installer", "arm64") == f"{base}/Git-2.60.0-arm64.exe"
        assert pick("portable", "amd64") == f"{base}/PortableGit-2.60.0-64-bit.7z.exe"
        assert pick("portable", "x86") is None
        # Only the project's own release downloads are ever accepted.
        assert pick("installer", "amd64", elsewhere) is None

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            ("", r"C:\Users\a\.local\bin"),
            (r"C:\tools;%USERPROFILE%\bin", r"C:\tools;%USERPROFILE%\bin;C:\Users\a\.local\bin"),
            ("C:\\tools;", r"C:\tools;C:\Users\a\.local\bin"),  # trailing separator kept as is
            # Already there, in any spelling: nothing to do.
            (r"C:\tools;C:\Users\a\.local\bin", None),
            (r"c:\users\A\.LOCAL\bin\;C:\tools", None),
            (r"C:\tools;%USERPROFILE%\.local\bin", None),
        ],
    )
    def test_user_path_gets_the_folder_once_and_keeps_every_existing_entry_exactly(
        self, shell: str, current: str, expected: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EB_CURRENT", current)
        monkeypatch.setenv("USERPROFILE", r"C:\Users\a")  # what %USERPROFILE% expands to
        value = self._ps(
            shell,
            "[string](Get-UserPathWithEntry -Current $env:EB_CURRENT"
            " -Folder 'C:\\Users\\a\\.local\\bin')",
        )
        assert (value or None) == expected

    @pytest.mark.parametrize(
        ("subject", "trusted"),
        [
            # The signer of dotnet-install.ps1, as Get-AuthenticodeSignature reported it.
            (
                "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US",
                True,
            ),
            ("CN=Microsoft Corporation, O=Microsoft Corporation Ltd, C=US", False),
            ("CN=Microsoft Corporation, O=Contoso, C=US", False),
            ('CN="x, O=Microsoft Corporation, y", O=Contoso, C=US', False),
        ],
    )
    def test_the_dotnet_install_script_must_be_signed_by_microsoft(
        self, shell: str, subject: str, trusted: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EB_SUBJECT", subject)
        value = self._ps(
            shell,
            "Test-TrustedPublisher -Status 'Valid' -Subject $env:EB_SUBJECT"
            " -Organization $MicrosoftPublisher",
        )
        assert value is trusted

    @pytest.mark.parametrize(
        ("listing", "major"),
        [
            ("9.0.321 [C:\\Users\\a\\AppData\\Local\\entrabot\\tools\\dotnet\\sdk]", 9),
            (
                "8.0.404 [C:\\Program Files\\dotnet\\sdk]\n"
                "10.0.100 [C:\\Program Files\\dotnet\\sdk]",
                10,
            ),
            ("", 0),
            ("No .NET SDKs were found.", 0),
        ],
        ids=["one", "newest-of-two", "none", "message"],
    )
    def test_the_newest_sdk_is_read_from_dotnets_own_listing(
        self, shell: str, listing: str, major: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EB_LISTING", listing)
        value = self._ps(
            shell, "Get-DotnetSdkMajor -Listing ([string]$env:EB_LISTING).Split([char]10)"
        )
        assert value == major

    def test_work_iq_tools_go_in_entrabots_own_folder(self, shell: str) -> None:
        value = self._ps(
            shell, "Get-WorkIqToolFolders -LocalAppData 'C:\\Users\\a\\AppData\\Local'"
        )
        assert value == {
            "Dotnet": r"C:\Users\a\AppData\Local\entrabot\tools\dotnet",
            "A365": r"C:\Users\a\AppData\Local\entrabot\tools\a365",
        }
        assert self._ps(shell, "Get-WorkIqToolFolders -LocalAppData ''") is None

    def test_git_bash_candidates_in_claude_codes_order(self, shell: str) -> None:
        value = self._ps(
            shell,
            "@(Get-GitBashCandidates -Override 'D:\\tools\\git\\bin\\bash.exe'"
            " -ProgramFiles 'C:\\Program Files' -LocalAppData 'C:\\Users\\a\\AppData\\Local'"
            " -GitOnPath 'E:\\Git\\cmd\\git.exe')",
        )
        assert value == [
            r"D:\tools\git\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Users\a\AppData\Local\Programs\Git\bin\bash.exe",
            r"E:\Git\bin\bash.exe",
        ]
        only_standard = self._ps(
            shell,
            "@(Get-GitBashCandidates -Override '' -ProgramFiles 'C:\\Program Files'"
            " -LocalAppData '' -GitOnPath '')",
        )
        assert only_standard == [r"C:\Program Files\Git\bin\bash.exe"]

    @pytest.mark.parametrize(
        ("bash", "git_on_path", "needs"),
        [
            # Claude Code finds these by itself.
            (r"C:\Program Files\Git\bin\bash.exe", "", False),
            (r"c:\program files\git\bin\bash.exe", "", False),
            (
                r"C:\Users\a\AppData\Local\Programs\Git\bin\bash.exe",
                r"C:\Users\a\AppData\Local\Programs\Git\cmd\git.exe",
                False,
            ),
            # A per-user copy that is not on PATH needs the override variable.
            (r"C:\Users\a\AppData\Local\Programs\Git\bin\bash.exe", "", True),
            (
                r"C:\Users\a\AppData\Local\Programs\Git\bin\bash.exe",
                r"D:\other\Git\cmd\git.exe",
                True,
            ),
        ],
    )
    def test_when_claude_code_needs_the_git_bash_override(
        self, shell: str, bash: str, git_on_path: str, needs: bool
    ) -> None:
        os.environ["EB_BASH"] = bash
        os.environ["EB_GIT"] = git_on_path
        try:
            value = self._ps(
                shell,
                "Test-GitBashNeedsOverride -BashPath $env:EB_BASH"
                " -ProgramFiles 'C:\\Program Files' -GitOnPath $env:EB_GIT",
            )
        finally:
            del os.environ["EB_BASH"]
            del os.environ["EB_GIT"]
        assert value is needs


def _run_prereqs_with_stubs(
    tmp_path: Path, shell: str, stubs: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run prereqs-windows.ps1 with machine-touching functions replaced by ``stubs``."""
    source = PREREQS_PS1.read_text(encoding="ascii")
    marker = "if ($Help) {\n"  # the first statement after every function definition
    assert source.count(marker) == 1
    script = tmp_path / "prereqs-windows.ps1"
    script.write_text(source.replace(marker, stubs + "\n" + marker), encoding="ascii")
    env = shell_env(shell)
    env.update(
        ProgramFiles=r"C:\Program Files",
        LOCALAPPDATA=r"C:\Users\a\AppData\Local",
        USERPROFILE=str(tmp_path),
        TEMP=str(tmp_path),
        POWERSHELL_TELEMETRY_OPTOUT="1",
        NO_COLOR="1",
    )
    env.pop("CLAUDE_CODE_GIT_BASH_PATH", None)
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


_COMMON_STUBS = """function Update-SessionPath { }
function Get-GitOnPath { return '' }
function Add-UserPathEntry {
    param([string]$Folder)
    Write-Host "SIM add to user PATH: $Folder"
    return $true
}
"""


@posix_only
@pytest.mark.parametrize("shell", shell_params)
class TestPrereqsSimulatedRun:
    def test_tools_that_already_run_are_left_alone(self, shell: str, tmp_path: Path) -> None:
        stubs = _COMMON_STUBS + r"""
function Find-GitBash {
    return @{
        Path = 'C:\Program Files\Git\bin\bash.exe'
        Version = 'GNU bash, version 5.2.37'
    }
}
function Find-ClaudeCode {
    return @{
        Path = 'C:\Users\a\.local\bin\claude.exe'
        Version = '2.1.322 (Claude Code)'
    }
}
function Install-GitWithInstaller { throw 'must not install' }
function Install-PortableGit { throw 'must not install' }
function Install-ClaudeCode { throw 'must not install' }
"""
        result = _run_prereqs_with_stubs(tmp_path, shell, stubs, "-WithGit", "-WithClaudeCode")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert (
            r"[ok] Git for Windows already installed: C:\Program Files\Git\bin\bash.exe"
            in result.stdout
        )
        claude_line = r"[ok] Claude Code already installed: C:\Users\a\.local\bin\claude.exe"
        assert claude_line in result.stdout
        assert "CLAUDE_CODE_GIT_BASH_PATH" not in result.stdout  # Claude Code finds this one itself
        # Claude is not in %USERPROFILE%\.local\bin here, so the PATH is left alone.
        assert "SIM add to user PATH" not in result.stdout

    def test_claude_in_its_native_folder_gets_that_folder_on_the_user_path(
        self, shell: str, tmp_path: Path
    ) -> None:
        native = tmp_path / ".local" / "bin"
        native.mkdir(parents=True)
        (native / "claude.exe").write_bytes(b"")
        stubs = _COMMON_STUBS + r"""
function Find-ClaudeCode { return @{ Path = 'claude.exe'; Version = '2.1.322 (Claude Code)' } }
function Install-ClaudeCode { throw 'must not install' }
"""
        result = _run_prereqs_with_stubs(tmp_path, shell, stubs, "-WithClaudeCode")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert "SIM add to user PATH:" in result.stdout
        assert "New terminal windows will find" in result.stdout

    def test_git_from_the_installer_is_only_reported_once_bash_runs(
        self, shell: str, tmp_path: Path
    ) -> None:
        stubs = _COMMON_STUBS + r"""
$script:simulatedGit = $false
function Find-GitBash {
    if ($script:simulatedGit) {
        return @{
            Path = 'C:\Users\a\AppData\Local\Programs\Git\bin\bash.exe'
            Version = 'GNU bash, version 5.2.37'
        }
    }
    return $null
}
function Install-GitWithInstaller { $script:simulatedGit = $true; return $true }
function Install-PortableGit { throw 'the second route must not run' }
"""
        result = _run_prereqs_with_stubs(tmp_path, shell, stubs, "-WithGit")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert (
            "[ok] Git for Windows installed for your account: "
            r"C:\Users\a\AppData\Local\Programs\Git\bin\bash.exe" in result.stdout
        )
        # A per-user copy that is not on PATH: Claude Code is told where it is.
        assert "Set CLAUDE_CODE_GIT_BASH_PATH" in result.stdout

    def test_portable_git_is_the_second_route(self, shell: str, tmp_path: Path) -> None:
        stubs = _COMMON_STUBS + r"""
$script:simulatedGit = $false
function Find-GitBash {
    if ($script:simulatedGit) {
        return @{
            Path = 'C:\Users\a\AppData\Local\Programs\Git\bin\bash.exe'
            Version = 'GNU bash, version 5.2.37'
        }
    }
    return $null
}
function Install-GitWithInstaller { Write-Note 'SIM: installer unavailable (1603)'; return $false }
function Install-PortableGit { $script:simulatedGit = $true; return $true }
"""
        result = _run_prereqs_with_stubs(tmp_path, shell, stubs, "-WithGit")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert "[ok] Git for Windows unpacked (portable) for your account:" in result.stdout

    def test_an_installer_that_returns_success_without_a_working_tool_is_a_failure(
        self, shell: str, tmp_path: Path
    ) -> None:
        stubs = _COMMON_STUBS + """
function Find-GitBash { return $null }
function Find-ClaudeCode { return $null }
function Install-GitWithInstaller { return $true }
function Install-PortableGit { return $true }
function Install-ClaudeCode { return $true }
"""
        result = _run_prereqs_with_stubs(tmp_path, shell, stubs, "-WithGit", "-WithClaudeCode")
        assert result.returncode == 1
        assert result.stderr == ""
        assert "Git for Windows could not be installed automatically" in result.stdout
        assert "Claude Code could not be installed automatically" in result.stdout
        assert "FAILED:            Git for Windows" in result.stdout
        assert "FAILED:            Claude Code" in result.stdout
        assert "[ok] Git for Windows" not in result.stdout


def _ps_bool(value: bool) -> str:
    return "$true" if value else "$false"


def _work_iq_stubs(
    *,
    pwsh: bool = True,
    winget: bool = True,
    a365: bool = True,
    sdk: bool = True,
    dotnet_installs: bool = True,
    a365_runs_after_install: bool = True,
) -> str:
    """The Work IQ block with the machine replaced: a365, a .NET SDK and PowerShell 7 there or
    not, winget usable or not, and every install only said (and remembered)."""
    return _COMMON_STUBS + f"""
$script:have = @{{ a365 = {_ps_bool(a365)}; sdk = {_ps_bool(sdk)}; pwsh = {_ps_bool(pwsh)} }}
function Test-CommandExists {{ param([string]$Name) return [bool]$script:have[$Name] }}
function Test-WingetWorks {{ return {_ps_bool(winget)} }}
function Install-WithWinget {{
    param([string]$Id, [string[]]$Extra = @())
    Write-Host "SIM winget $Id"
    if ($Id -eq 'Microsoft.PowerShell') {{ $script:have['pwsh'] = $true }}
    return $true
}}
function Find-A365 {{
    param([string]$PrivateFolder, [string]$PrivateDotnet)
    if (-not $script:have['a365']) {{ return $null }}
    return @{{ Path = (Join-WindowsPath $PrivateFolder 'a365.exe'); Version = '2.0.4+6b9a2a1b83' }}
}}
function Get-DotnetSdk {{
    param([string]$PrivateFolder)
    if (-not $script:have['sdk']) {{ return $null }}
    $exe = Join-WindowsPath $PrivateFolder 'dotnet.exe'
    return @{{ Exe = $exe; Root = $PrivateFolder; Major = 10 }}
}}
function Install-PrivateDotnet {{
    param([string]$Folder)
    Write-Host "SIM dotnet-install into $Folder"
    if (-not {_ps_bool(dotnet_installs)}) {{ return $false }}
    $script:have['sdk'] = $true
    return $true
}}
function Use-DotnetSdk {{ param($Sdk) Write-Host "SIM use $($Sdk.Exe)" }}
function Install-A365 {{
    param($Sdk, [string]$Folder)
    Write-Host "SIM a365 into $Folder with $($Sdk.Exe)"
    if ({_ps_bool(a365_runs_after_install)}) {{ $script:have['a365'] = $true }}
    return $true
}}
function dotnet {{ Write-Host "SIM dotnet $args"; $global:LASTEXITCODE = 0 }}
"""


_TOOLS = r"C:\Users\a\AppData\Local\entrabot\tools"


@posix_only
@pytest.mark.parametrize("shell", shell_params)
class TestPrereqsWorkIq:
    """-WithWorkIq installs everything Word, Excel and OneDrive files need, PowerShell 7 too:
    one Agent 365 CLI step needs it, and the setup wizard offers this switch for Work IQ."""

    def test_a_missing_powershell_7_is_installed_with_winget(
        self, shell: str, tmp_path: Path
    ) -> None:
        result = _run_prereqs_with_stubs(
            tmp_path, shell, _work_iq_stubs(pwsh=False, winget=True), "-WithWorkIq"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert "SIM winget Microsoft.PowerShell" in result.stdout
        assert "[ok] PowerShell 7 installed" in result.stdout
        assert "newly installed:   PowerShell 7" in result.stdout

    def test_one_already_there_is_left_alone(self, shell: str, tmp_path: Path) -> None:
        result = _run_prereqs_with_stubs(
            tmp_path, shell, _work_iq_stubs(pwsh=True, winget=True), "-WithWorkIq"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SIM winget Microsoft.PowerShell" not in result.stdout
        assert "[ok] PowerShell 7 is already installed" in result.stdout

    def test_without_winget_it_says_where_to_get_it_and_fails(
        self, shell: str, tmp_path: Path
    ) -> None:
        result = _run_prereqs_with_stubs(
            tmp_path, shell, _work_iq_stubs(pwsh=False, winget=False), "-WithWorkIq"
        )
        assert result.returncode == 1
        assert "SIM winget" not in result.stdout
        assert (
            "Install PowerShell 7 from "
            "https://learn.microsoft.com/powershell/scripting/install/install-powershell-on-windows"
        ) in result.stdout
        assert "FAILED:            PowerShell 7" in result.stdout

    def test_an_a365_that_runs_is_left_alone(self, shell: str, tmp_path: Path) -> None:
        result = _run_prereqs_with_stubs(tmp_path, shell, _work_iq_stubs(), "-WithWorkIq")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert "[ok] The Agent 365 CLI is already installed:" in result.stdout
        assert "2.0.4+6b9a2a1b83" in result.stdout
        assert "SIM dotnet-install" not in result.stdout and "SIM a365 into" not in result.stdout
        assert "already installed: Agent 365 CLI (a365)" in result.stdout

    def test_with_a_dotnet_sdk_a365_goes_into_entrabots_folder_with_it(
        self, shell: str, tmp_path: Path
    ) -> None:
        result = _run_prereqs_with_stubs(
            tmp_path, shell, _work_iq_stubs(a365=False), "-WithWorkIq"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert "SIM dotnet-install" not in result.stdout  # the SDK there is used as it is
        assert f"SIM a365 into {_TOOLS}\\a365 with {_TOOLS}\\dotnet\\dotnet.exe" in result.stdout
        assert "newly installed:   Agent 365 CLI (a365)" in result.stdout
        assert "SIM winget" not in result.stdout  # never winget for .NET or a365

    def test_without_one_dotnet_is_installed_there_first(self, shell: str, tmp_path: Path) -> None:
        result = _run_prereqs_with_stubs(
            tmp_path, shell, _work_iq_stubs(a365=False, sdk=False), "-WithWorkIq"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        out = result.stdout
        assert out.index(f"SIM dotnet-install into {_TOOLS}\\dotnet") < out.index("SIM a365 into")
        assert out.index("SIM use") < out.index("SIM a365 into")  # DOTNET_ROOT before a365 runs
        assert "newly installed:   .NET SDK" in out
        assert "newly installed:   Agent 365 CLI (a365)" in out
        assert "SIM winget" not in out

    def test_a_dotnet_that_cannot_be_installed_stops_a365_too(
        self, shell: str, tmp_path: Path
    ) -> None:
        stubs = _work_iq_stubs(a365=False, sdk=False, dotnet_installs=False)
        result = _run_prereqs_with_stubs(tmp_path, shell, stubs, "-WithWorkIq")
        assert result.returncode == 1
        assert "SIM a365 into" not in result.stdout
        assert "FAILED:            .NET SDK" in result.stdout
        assert "FAILED:            Agent 365 CLI (a365)" in result.stdout
        assert "https://learn.microsoft.com/dotnet/core/install/windows" in result.stdout

    def test_an_a365_that_does_not_run_after_its_install_is_a_failure(
        self, shell: str, tmp_path: Path
    ) -> None:
        stubs = _work_iq_stubs(a365=False, a365_runs_after_install=False)
        result = _run_prereqs_with_stubs(tmp_path, shell, stubs, "-WithWorkIq")
        assert result.returncode == 1
        assert "FAILED:            Agent 365 CLI (a365)" in result.stdout
        assert "newly installed:   Agent 365 CLI" not in result.stdout

    def test_winget_is_only_checked_when_powershell_7_is_missing(
        self, shell: str, tmp_path: Path
    ) -> None:
        result = _run_prereqs_with_stubs(
            tmp_path, shell, _work_iq_stubs(pwsh=True, winget=False), "-WithWorkIq"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "winget) is not usable" not in result.stdout
