"""Static tests for setup-windows.ps1 Step 6, which generated a certificate and never registered it.

PowerShell isn't run where most of this runs, so these read the script: what it calls, in what
order, and whether each exit code is looked at. The parse check needs pwsh (Windows CI has it).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from tests.scripts._blueprint_cert_helpers import REPO_ROOT

SETUP_PS1 = REPO_ROOT / "scripts" / "setup-windows.ps1"
SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"
TEXT = SETUP_PS1.read_text(encoding="utf-8-sig")
REGISTER = "register_windows_blueprint_cert.py"
HELPERS = ["verify_blueprint_cert.py", "generate_windows_cert.py", REGISTER]


def _call(helper: str) -> int:
    return TEXT.index(f"(Join-Path $ScriptDir '{helper}')")


def _between(start: str, end: str) -> str:
    begin = TEXT.index(start)
    return TEXT[begin : TEXT.index(end, begin)]


def test_step_6_verifies_the_local_cert_and_registers_with_the_blueprint_object_id() -> None:
    assert "$BlueprintObjectId $x5tS256" in TEXT  # verify_blueprint_cert.py <object id> <x5t#S256>
    assert f"{REGISTER}') --blueprint-object-id $BlueprintObjectId --der $derPath" in TEXT
    assert TEXT.index("if (-not $BlueprintObjectId)") < _call("verify_blueprint_cert.py")


def test_a_new_cert_is_registered_before_it_is_written_to_env() -> None:
    env_write = TEXT.index("Set-Content -Path $envPath")
    assert _call("generate_windows_cert.py") < TEXT.index("$generated = $true") < _call(REGISTER)
    assert _call(REGISTER) < env_write


def test_the_verify_exit_code_means_reuse_register_or_stop() -> None:
    reuse = _between("if ($verifyExit -eq 0)", "elseif ($verifyExit -eq 1)")
    register = _between("elseif ($verifyExit -eq 1)", "} else {")
    stop = _between("# A failed check", "\n        }")
    assert "$register = $false" in reuse
    assert "WriteAllBytes($derPath" in register and "$generated" not in register
    assert "Fail " in stop and "generate_windows_cert" not in stop


@pytest.mark.parametrize("helper", HELPERS)
def test_the_statement_after_each_helper_reads_its_exit_code(helper: str) -> None:
    lines = TEXT[_call(helper) :].split("\n")
    last = 0
    while lines[last].rstrip().endswith("`"):  # a call continued over several lines
        last += 1
    assert "$LASTEXITCODE" in lines[last + 1]


def test_a_non_zero_exit_is_handled_rather_than_thrown() -> None:
    # Set-StrictMode and $ErrorActionPreference = 'Stop' are in force, so this keeps an
    # operator's $PSNativeCommandUseErrorActionPreference = $true from turning
    # "exit 1: not on the Blueprint" into a terminating error before it is handled.
    assert "$PSNativeCommandUseErrorActionPreference = $false" in TEXT
    assert TEXT.index("$PSNativeCommandUseErrorActionPreference") < _call(
        "verify_blueprint_cert.py"
    )
    assert ".Matches[0]" not in _between("Step 6 ", 'Step 7 "')  # throws on missing output


def test_only_a_generated_cert_is_removed_when_nothing_was_registered() -> None:
    assert TEXT.count('Remove-Item "Cert:\\CurrentUser\\My\\') == 1
    assert "Fail " in _between("if ($generated) { Remove-Item", "} finally")
    assert "Fail" not in _between("elseif ($registerExit -eq 3)", "} else {")  # exit 3 keeps it


def test_an_unconfirmed_registration_is_recorded_in_env_before_setup_stops() -> None:
    assert TEXT.index("icacls $envPath") < TEXT.index("if ($unconfirmed)")
    assert "re-run setup" in _between("if ($unconfirmed)", "\n}\n").lower()


def test_the_temporary_certificate_file_is_deleted_on_every_path() -> None:
    assert re.search(r"finally\s*\{[^}]*Remove-Item \$derPath", TEXT)


def _env_block() -> str:
    return _between('Step 7 "Writing .env"', "icacls $envPath")


def test_env_holds_what_setup_sh_writes_for_the_runtime() -> None:
    # config.py reads identity only from the environment and .env, never from the state
    # file, so a key missing here is an identity the agent can't sign in with.
    script = SETUP_SH.read_text(encoding="utf-8")
    heredoc = script[script.index("cat > .env << EOF") :]
    heredoc = heredoc[: heredoc.index("\nEOF\n")]
    keys = set(re.findall(r"^(ENTRABOT_[A-Z_]+)=", heredoc, re.M)) - {"ENTRABOT_LOG_LEVEL"}
    assert len(keys) > 10
    block = _env_block()
    assert [key for key in sorted(keys) if key not in block] == []


@pytest.mark.parametrize(
    "state_key",
    ["BLUEPRINT_APP_ID", "AGENT_ID", "AGENT_OBJECT_ID", "AGENT_USER_ID", "AGENT_USER_UPN"],
)
def test_identity_values_come_from_the_state_file(state_key: str) -> None:
    assert f"$state.{state_key}" in TEXT
    assert TEXT.index(f"$state.{state_key}") < TEXT.index('Step 7 "Writing .env"')


def test_env_is_written_on_every_run_and_replaces_earlier_lines() -> None:
    block = _env_block()
    assert "-Append" not in block and "Set-Content" in block
    # A machine set up before this fix has a registered cert to reuse and no identity in
    # .env, so reusing a cert must not skip this.
    assert "if ($generated)" not in block
    # config.py keeps the FIRST value of a repeated key, so a stale line left above a new
    # one would win.
    assert '$line.StartsWith("$_=")' in block


def test_the_sponsor_is_defaulted_only_when_env_names_none() -> None:
    # The account running setup isn't always the sponsor, so a re-run must not change it.
    block = _env_block()
    guard = "if ($envText -notmatch '(?m)^ENTRABOT_HUMAN_USER_ID=\\S')"
    assert guard in block
    assert block.index(guard) < block.index("ENTRABOT_HUMAN_USER_ID'")
    assert "ENTRABOT_HUMAN" not in block[: block.index(guard)]
    assert "az ad signed-in-user show --output json" in block  # JSON, never TSV (AGENTS.md)


def test_the_script_no_longer_claims_provisioning_publishes_the_cert() -> None:
    text = " ".join(TEXT.split())
    assert "already publishes the cert" not in text
    assert "PATCH it into the Blueprint" not in text
    assert REGISTER in TEXT.split("#>")[0]  # the header describes what now happens


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell 7 (pwsh)")
def test_setup_windows_script_parses_without_errors() -> None:
    command = (  # the path travels in the environment, so quoting it can't go wrong
        "$tokens = $null; $errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "$env:SETUP_PS1_UNDER_TEST, [ref]$tokens, [ref]$errors); "
        "$errors | ForEach-Object { 'line ' + $_.Extent.StartLineNumber + ': ' + $_.Message }; "
        "if ($errors.Count -gt 0) { exit 1 }"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "SETUP_PS1_UNDER_TEST": str(SETUP_PS1)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
