"""Run setup-windows.ps1 Steps 6-8 under real PowerShell, with the outside world stubbed.

The static checks in test_setup_windows_cert_step.py read the script. These execute its text,
so a strict-mode error or a wrong branch fails here. They need pwsh: Windows CI has it. Python
helpers, az, icacls and the certificate store are replaced with stubs, so no real Graph call,
certificate or .env is touched, and any OS with pwsh can run them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.scripts._blueprint_cert_helpers import REPO_ROOT

pytestmark = pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell 7 (pwsh)")

SETUP_PS1 = REPO_ROOT / "scripts" / "setup-windows.ps1"
TEXT = SETUP_PS1.read_text(encoding="utf-8-sig")
STEPS = TEXT[TEXT.index('Step 6 "Blueprint cert') : TEXT.index('Step 9 "Registering MCP server"')]

NEW_SHA1 = "A" * 40
NEW_X5T = "n" * 43
OLD_SHA1 = "B" * 40
OLD_X5T = "o" * 43
CERT_LINES = [
    f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={OLD_X5T}",
    f"ENTRABOT_BLUEPRINT_CERT_SHA1={OLD_SHA1}",
    "ENTRABOT_BLUEPRINT_KSP=software",
]
CLOUD_LINES = [
    "ENTRABOT_BLOB_ENDPOINT=https://old.example.invalid",
    "ENTRABOT_BLOB_CONTAINER=old-container",
]
LOCAL_LINE = "ENTRABOT_KEEP_MEMORY_LOCAL=true"

PRELUDE = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Work = $env:STEPS_WORK_DIR
$ProjectRoot = $Work
$ScriptDir = Join-Path $Work 'scripts'
$env:TEMP = $Work
$VenvPython = 'Invoke-FakePython'
$BlueprintAppId = 'blueprint-app'
$BlueprintObjectId = 'blueprint-object'
$AgentId = 'agent-app'
$AgentObjectId = 'agent-object'
$AgentUserId = $env:FAKE_AGENT_USER_ID
$StateAgentUserUpn = 'agent@example.invalid'
$ProvisionerClientId = 'provisioner-app'
$UseCloudMemory = [bool]$env:FAKE_USE_CLOUD_MEMORY
$WithStorageAccount = ''
$WithContainer = ''
$CreateNewStorage = $false
$account = [pscustomobject]@{
    tenantId = 'tenant'
    user     = [pscustomobject]@{ name = 'runner@example.invalid' }
}

function Note($text) { Add-Content -Path (Join-Path $Work 'calls.log') -Value $text }
function Step($n, $msg) { Note "step $n" }
function Success($msg) { Note "success $msg" }
function Fail($msg) { Note "fail $msg"; exit 7 }

function Invoke-FakePython {
    $helper = Split-Path $args[0] -Leaf
    Note "python $helper"
    $exit = 0
    switch ($helper) {
        'verify_blueprint_cert.py' { $exit = [int]$env:FAKE_VERIFY_EXIT }
        'generate_windows_cert.py' {
            $der = $args[$args.IndexOf('--export-der') + 1]
            [IO.File]::WriteAllBytes($der, [byte[]](1, 2, 3))
            $env:FAKE_GENERATE_OUTPUT -split '\|'
        }
        'register_windows_blueprint_cert.py' {
            $der = $args[$args.IndexOf('--der') + 1]
            Note "registered bytes $([IO.File]::ReadAllBytes($der).Length)"
            $exit = [int]$env:FAKE_REGISTER_EXIT
        }
        'provision_blob_storage.py' {
            if ($env:FAKE_PROVISION_OUTPUT) { $env:FAKE_PROVISION_OUTPUT -split '\|' }
            $exit = [int]$env:FAKE_PROVISION_EXIT
        }
    }
    $global:LASTEXITCODE = $exit
}

function az {
    Note "az $($args -join ' ')"
    if ($env:FAKE_AZ_EXIT -ne '0') { $global:LASTEXITCODE = 1; return }
    $global:LASTEXITCODE = 0
    '{"id":"runner-object"}'
}
function icacls { Note 'icacls'; $global:LASTEXITCODE = 0 }

function Test-Path {
    param([string]$Path)
    if ($Path -like 'Cert:*') { return [bool]$env:FAKE_CERT_IN_STORE }
    Microsoft.PowerShell.Management\Test-Path -LiteralPath $Path
}
function Get-Item {
    param([string]$Path)
    $now = Get-Date
    $state = $env:FAKE_CERT_STATE
    $cert = [pscustomobject]@{
        HasPrivateKey = $state -ne 'no-private-key'
        NotBefore     = $now.AddDays($(if ($state -eq 'not-yet-valid') { 1 } else { -1 }))
        NotAfter      = $now.AddDays($(if ($state -eq 'expired') { -1 } else { 1 }))
    }
    $cert | Add-Member -MemberType ScriptMethod -Name GetRawCertData -Value { [byte[]](1, 2, 3, 4) }
    $cert
}
function Remove-Item {
    param([string]$Path, [switch]$Force)
    if ($Path -like 'Cert:*') { Note "removed cert $Path"; return }
    Microsoft.PowerShell.Management\Remove-Item -LiteralPath $Path -Force
}
"""


@dataclass
class Run:
    exit_code: int
    env: list[str]
    calls: list[str]
    output: str
    leftover_certs: list[Path]

    def called(self, helper: str) -> bool:
        return f"python {helper}" in self.calls

    def values(self, key: str) -> list[str]:
        return [line.split("=", 1)[1] for line in self.env if line.startswith(f"{key}=")]

    def effective(self, key: str) -> str | None:
        """The value config.py would use: trimmed, skipping comments, first one wins."""
        for raw in self.env:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip()
        return None


@pytest.fixture
def run_steps(tmp_path: Path):
    script = tmp_path / "steps.ps1"
    script.write_text(PRELUDE + STEPS, encoding="utf-8")
    env_path = tmp_path / ".env"

    def _run(
        env: list[str] | None = None,
        *,
        cert_in_store: bool = False,
        cert_state: str = "usable",
        verify: int = 0,
        register: int = 0,
        use_cloud: bool = False,
        agent_user: str = "agent-user",
        provision: int = 0,
        provision_output: str = "BLOB_ENDPOINT=https://new.example.invalid|BLOB_CONTAINER=new",
        az_exit: int = 0,
    ) -> Run:
        if env is not None:
            env_path.write_text("\n".join(env) + "\n", encoding="utf-8")
        calls_path = tmp_path / "calls.log"
        calls_path.unlink(missing_ok=True)
        fake = {
            "NO_COLOR": "1",  # keeps escape codes out of a failure's output
            "STEPS_WORK_DIR": str(tmp_path),
            "FAKE_CERT_IN_STORE": "1" if cert_in_store else "",
            "FAKE_CERT_STATE": cert_state,
            "FAKE_VERIFY_EXIT": str(verify),
            "FAKE_REGISTER_EXIT": str(register),
            "FAKE_USE_CLOUD_MEMORY": "1" if use_cloud else "",
            "FAKE_AGENT_USER_ID": agent_user,
            "FAKE_PROVISION_EXIT": str(provision),
            "FAKE_PROVISION_OUTPUT": provision_output,
            "FAKE_AZ_EXIT": str(az_exit),
            "FAKE_GENERATE_OUTPUT": f"thumbprint={NEW_SHA1}|ksp=tpm|x5t_s256={NEW_X5T}",
        }
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=tmp_path,
            env={**os.environ, **fake},
        )
        return Run(
            exit_code=result.returncode,
            env=env_path.read_text(encoding="utf-8-sig").splitlines() if env_path.exists() else [],
            calls=calls_path.read_text(encoding="utf-8").splitlines()
            if calls_path.exists()
            else [],
            output=result.stdout + result.stderr,
            leftover_certs=list(tmp_path.glob("entrabot-blueprint-*.cer")),
        )

    return _run


class TestCertificateStep:
    def test_a_fresh_machine_generates_registers_and_records_the_certificate(self, run_steps):
        run = run_steps()
        assert run.exit_code == 0, run.output
        assert run.called("generate_windows_cert.py")
        assert run.called("register_windows_blueprint_cert.py")
        assert "registered bytes 3" in run.calls
        assert run.values("ENTRABOT_BLUEPRINT_CERT_SHA1") == [NEW_SHA1]
        assert run.values("ENTRABOT_BLUEPRINT_CERT_THUMBPRINT") == [NEW_X5T]
        assert run.values("ENTRABOT_BLUEPRINT_KSP") == ["tpm"]
        assert run.leftover_certs == []

    def test_a_registered_certificate_is_reused_and_nothing_is_written_to_the_blueprint(
        self, run_steps
    ):
        run = run_steps(CERT_LINES, cert_in_store=True, verify=0)
        assert run.exit_code == 0, run.output
        assert run.called("verify_blueprint_cert.py")
        assert not run.called("generate_windows_cert.py")
        assert not run.called("register_windows_blueprint_cert.py")
        assert run.values("ENTRABOT_BLUEPRINT_CERT_SHA1") == [OLD_SHA1]

    def test_a_certificate_missing_from_the_blueprint_is_registered_without_a_new_one(
        self, run_steps
    ):
        run = run_steps(CERT_LINES, cert_in_store=True, verify=1)
        assert run.exit_code == 0, run.output
        assert not run.called("generate_windows_cert.py")
        assert "registered bytes 4" in run.calls  # the bytes of the certificate in the store
        assert run.values("ENTRABOT_BLUEPRINT_CERT_SHA1") == [OLD_SHA1]
        assert run.values("ENTRABOT_BLUEPRINT_CERT_THUMBPRINT") == [OLD_X5T]
        assert run.leftover_certs == []

    def test_a_failed_check_changes_nothing(self, run_steps):
        run = run_steps(CERT_LINES, cert_in_store=True, verify=3)
        assert run.exit_code == 7, run.output
        assert not run.called("generate_windows_cert.py")
        assert not run.called("register_windows_blueprint_cert.py")
        assert run.env == CERT_LINES

    def test_a_certificate_in_env_but_not_the_store_is_replaced(self, run_steps):
        run = run_steps(CERT_LINES, cert_in_store=False)
        assert run.exit_code == 0, run.output
        assert run.called("generate_windows_cert.py")
        assert run.values("ENTRABOT_BLUEPRINT_CERT_SHA1") == [NEW_SHA1]

    @pytest.mark.parametrize("state", ["expired", "no-private-key", "not-yet-valid"])
    def test_a_certificate_that_cannot_sign_is_replaced(self, run_steps, state):
        # Registering or reusing it would report success while Hop 1 stays broken.
        run = run_steps(CERT_LINES, cert_in_store=True, cert_state=state)
        assert run.exit_code == 0, run.output
        assert not run.called("verify_blueprint_cert.py")
        assert run.called("generate_windows_cert.py")
        assert run.called("register_windows_blueprint_cert.py")
        assert run.values("ENTRABOT_BLUEPRINT_CERT_SHA1") == [NEW_SHA1]

    def test_a_padded_certificate_line_is_read_the_way_the_runtime_reads_it(self, run_steps):
        run = run_steps(
            [
                f" ENTRABOT_BLUEPRINT_CERT_THUMBPRINT = {OLD_X5T} ",
                f"ENTRABOT_BLUEPRINT_CERT_SHA1 = {OLD_SHA1}",
                "ENTRABOT_BLUEPRINT_KSP = software",
            ],
            cert_in_store=True,
            verify=0,
        )
        assert run.exit_code == 0, run.output
        assert not run.called("generate_windows_cert.py")
        assert run.effective("ENTRABOT_BLUEPRINT_CERT_SHA1") == OLD_SHA1

    def test_a_refused_registration_removes_the_new_certificate_and_writes_nothing(self, run_steps):
        run = run_steps(["KEEP=me"], register=1)
        assert run.exit_code == 7, run.output
        assert f"removed cert Cert:\\CurrentUser\\My\\{NEW_SHA1}" in run.calls
        assert run.env == ["KEEP=me"]
        assert run.leftover_certs == []

    def test_a_refused_registration_keeps_a_certificate_it_did_not_generate(self, run_steps):
        run = run_steps(CERT_LINES, cert_in_store=True, verify=1, register=1)
        assert run.exit_code == 7, run.output
        assert not [call for call in run.calls if call.startswith("removed cert")]
        assert run.env == CERT_LINES
        assert run.leftover_certs == []

    def test_an_unconfirmed_registration_records_the_certificate_and_then_stops(self, run_steps):
        run = run_steps(register=3)
        assert run.exit_code == 7, run.output
        assert not [call for call in run.calls if call.startswith("removed cert")]
        assert run.values("ENTRABOT_BLUEPRINT_CERT_SHA1") == [NEW_SHA1]
        assert "step 8" not in run.calls  # stops before the memory step
        assert run.leftover_certs == []


class TestEnvStep:
    def test_identity_is_written_and_repeated_keys_are_replaced_in_place(self, run_steps):
        run = run_steps(["ENTRABOT_AGENT_ID=stale", "ENTRABOT_AGENT_ID=staler", "OTHER=kept"])
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_AGENT_ID") == ["agent-app"]
        assert run.values("ENTRABOT_AGENT_USER_ID") == ["agent-user"]
        assert run.values("ENTRABOT_BLUEPRINT_OBJECT_ID") == ["blueprint-object"]
        assert run.values("ENTRABOT_TENANT_ID") == ["tenant"]
        assert "OTHER=kept" in run.env

    def test_a_value_that_is_missing_keeps_what_env_already_has(self, run_steps):
        run = run_steps(["ENTRABOT_AGENT_USER_ID=already-here"], agent_user="")
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_AGENT_USER_ID") == ["already-here"]

    def test_the_signed_in_account_becomes_the_sponsor_when_env_names_none(self, run_steps):
        run = run_steps()
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["runner-object"]
        assert run.values("ENTRABOT_HUMAN_UPN") == ["runner@example.invalid"]

    def test_a_re_run_does_not_change_the_sponsor(self, run_steps):
        run = run_steps(["ENTRABOT_HUMAN_USER_ID=the-sponsor"])
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["the-sponsor"]
        assert run.values("ENTRABOT_HUMAN_UPN") == []
        assert not [call for call in run.calls if call.startswith("az ")]

    def test_a_failed_sponsor_lookup_warns_and_setup_continues(self, run_steps):
        run = run_steps(az_exit=1)
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_HUMAN_USER_ID") == []
        assert run.values("ENTRABOT_AGENT_ID") == ["agent-app"]

    def test_both_upn_keys_name_the_selected_agent(self, run_steps):
        # config.py prefers ENTRABOT_AGENT_UPN, so a stale one would outrank the alias and
        # the email poll's self-sender filter would stop recognizing the agent's own mail.
        run = run_steps(["ENTRABOT_AGENT_UPN=old-agent@example.invalid"])
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_AGENT_UPN") == ["agent@example.invalid"]
        assert run.values("ENTRABOT_AGENT_USER_UPN") == ["agent@example.invalid"]

    @pytest.mark.parametrize(
        "line",
        [
            "ENTRABOT_HUMAN_USER_IDS=sponsor-a,sponsor-b",
            "ENTRABOT_HUMAN_UPN=sponsor@contoso.com",
            "ENTRABOT_HUMAN_UPNS=sponsor@contoso.com,other@contoso.com",
            " ENTRABOT_HUMAN_USER_ID = the-sponsor ",
        ],
    )
    def test_any_sponsor_key_counts_as_naming_a_sponsor(self, run_steps, line):
        run = run_steps(["ENTRABOT_AGENT_USER_ID=agent-user", line])
        assert run.exit_code == 0, run.output
        assert line in run.env
        assert not [call for call in run.calls if call.startswith("az ")]
        assert run.values("ENTRABOT_HUMAN_USER_TYPES") == []

    def test_a_commented_out_sponsor_does_not_count(self, run_steps):
        run = run_steps(["# ENTRABOT_HUMAN_USER_ID=old-sponsor"])
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["runner-object"]

    def test_a_run_for_a_different_agent_does_not_keep_the_old_sponsor(self, run_steps):
        run = run_steps(
            ["ENTRABOT_AGENT_USER_ID=another-agent", "ENTRABOT_HUMAN_USER_ID=old-sponsor"]
        )
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_HUMAN_USER_ID") == ["runner-object"]
        assert run.values("ENTRABOT_AGENT_USER_ID") == ["agent-user"]

    def test_a_different_agent_loses_the_old_sponsor_even_if_the_lookup_fails(self, run_steps):
        # Otherwise daily summaries for the new agent could reach the previous sponsor.
        run = run_steps(
            [
                "ENTRABOT_AGENT_USER_ID=another-agent",
                "ENTRABOT_HUMAN_USER_ID=old-sponsor",
                "ENTRABOT_HUMAN_UPNS=old@contoso.com",
            ],
            az_exit=1,
        )
        assert run.exit_code == 0, run.output
        assert run.effective("ENTRABOT_HUMAN_USER_ID") is None
        assert run.effective("ENTRABOT_HUMAN_UPNS") is None

    def test_a_different_agent_loses_the_old_blob_store_even_if_setup_stops_early(self, run_steps):
        # An unconfirmed certificate stops setup before Step 8. The next run sees this
        # agent's ID in .env, so the old blob lines would pass for its own.
        lines = ["ENTRABOT_AGENT_USER_ID=another-agent", *CLOUD_LINES]
        run = run_steps(lines, register=3)
        assert run.exit_code == 7, run.output
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") is None
        assert run.effective("ENTRABOT_BLOB_CONTAINER") is None

        again = run_steps()
        assert again.exit_code == 0, again.output
        assert again.effective("ENTRABOT_KEEP_MEMORY_LOCAL") == "true"

    def test_a_new_sponsor_does_not_inherit_the_old_sponsors_tenant_ids(self, run_steps):
        run = run_steps(
            ["ENTRABOT_AGENT_USER_ID=another-agent", "ENTRABOT_HUMAN_USER_TENANT_IDS=old-tenant"]
        )
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_HUMAN_USER_TENANT_IDS") == [""]

    def test_padded_keys_are_replaced_and_comments_are_left_alone(self, run_steps):
        # config.py trims keys, so a padded stale line above the new one would win.
        run = run_steps(
            ["ENTRABOT_AGENT_ID = stale", "  ENTRABOT_TENANT_ID=old", "# ENTRABOT_AGENT_ID=a note"]
        )
        assert run.exit_code == 0, run.output
        assert run.effective("ENTRABOT_AGENT_ID") == "agent-app"
        assert run.effective("ENTRABOT_TENANT_ID") == "tenant"
        assert "# ENTRABOT_AGENT_ID=a note" in run.env


class TestMemoryStep:
    def test_a_new_machine_defaults_to_local_memory(self, run_steps):
        run = run_steps()
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]
        assert not run.called("provision_blob_storage.py")

    def test_re_running_without_the_flag_keeps_a_machine_on_cloud_memory(self, run_steps):
        # The certificate repair says to re-run setup, and doesn't say to pass -UseCloudMemory.
        # ENTRABOT_KEEP_MEMORY_LOCAL=true outranks the blob settings at runtime, so writing it
        # would silently move the agent to an empty local store.
        run = run_steps(CLOUD_LINES)
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://old.example.invalid"]
        assert run.values("ENTRABOT_BLOB_CONTAINER") == ["old-container"]

    def test_re_running_without_the_flag_keeps_a_machine_on_local_memory_once(self, run_steps):
        first = run_steps()
        assert first.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]
        second = run_steps()
        assert second.exit_code == 0, second.output
        assert second.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]
        assert second.env.count("# ADR-005: keep agent memory local (skip cloud sync)") == 1

    def test_local_stays_local_when_env_has_both_settings(self, run_steps):
        # The local flag outranks the blob settings at runtime, so this machine is local today.
        run = run_steps([*CLOUD_LINES, LOCAL_LINE])
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://old.example.invalid"]

    def test_padded_cloud_settings_count_as_cloud(self, run_steps):
        lines = [
            "  ENTRABOT_BLOB_ENDPOINT = https://old.example.invalid",
            "ENTRABOT_BLOB_CONTAINER = c",
        ]
        run = run_steps(lines)
        assert run.exit_code == 0, run.output
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") is None
        assert run.effective("ENTRABOT_BLOB_CONTAINER") == "c"

    @pytest.mark.parametrize(
        "line", ["ENTRABOT_KEEP_MEMORY_LOCAL = true", "ENTRABOT_KEEP_MEMORY_LOCAL=YES"]
    )
    def test_a_local_flag_counts_however_it_is_written(self, run_steps, line):
        run = run_steps([*CLOUD_LINES, line])
        assert run.exit_code == 0, run.output
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") in ("true", "YES")

    def test_opting_in_replaces_a_padded_local_flag(self, run_steps):
        run = run_steps(["ENTRABOT_KEEP_MEMORY_LOCAL = true"], use_cloud=True)
        assert run.exit_code == 0, run.output
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") is None
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") == "https://new.example.invalid"

    def test_the_first_value_of_a_repeated_flag_decides(self, run_steps):
        run = run_steps(
            [*CLOUD_LINES, "ENTRABOT_KEEP_MEMORY_LOCAL=false", "ENTRABOT_KEEP_MEMORY_LOCAL=true"]
        )
        assert run.exit_code == 0, run.output
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") == "false"
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") == "https://old.example.invalid"

    def test_a_run_for_a_different_agent_does_not_keep_the_old_blob_store(self, run_steps):
        # The container is scoped to one Agent User, so the new one couldn't reach it.
        run = run_steps(["ENTRABOT_AGENT_USER_ID=another-agent", *CLOUD_LINES])
        assert run.exit_code == 0, run.output
        assert run.effective("ENTRABOT_KEEP_MEMORY_LOCAL") == "true"
        assert run.effective("ENTRABOT_BLOB_ENDPOINT") is None

    def test_half_a_cloud_configuration_is_not_treated_as_cloud(self, run_steps):
        run = run_steps(CLOUD_LINES[:1])
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]

    def test_opting_in_replaces_earlier_memory_lines(self, run_steps):
        # config.py keeps the first value of a repeated key, and the local flag outranks the
        # blob settings, so an appended line would leave the new store unused.
        run = run_steps([*CLOUD_LINES, LOCAL_LINE], use_cloud=True)
        assert run.exit_code == 0, run.output
        assert run.called("provision_blob_storage.py")
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://new.example.invalid"]
        assert run.values("ENTRABOT_BLOB_CONTAINER") == ["new"]

    def test_opting_in_on_a_new_machine_writes_the_blob_settings(self, run_steps):
        run = run_steps(use_cloud=True)
        assert run.exit_code == 0, run.output
        assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://new.example.invalid"]
        assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []

    @pytest.mark.parametrize(
        "failure",
        [
            {"provision": 1, "provision_output": ""},
            {"provision": 0, "provision_output": ""},
            {"provision": 0, "provision_output": "BLOB_ENDPOINT=https://new.example.invalid"},
            {"agent_user": ""},
        ],
        ids=["provisioning-fails", "no-output", "half-output", "no-agent-user"],
    )
    class TestWhenABlobStoreCannotBeProvisioned:
        def test_a_machine_on_cloud_memory_stays_there(self, run_steps, failure):
            run = run_steps(
                [*CLOUD_LINES, "ENTRABOT_AGENT_USER_ID=agent-user"], use_cloud=True, **failure
            )
            assert run.exit_code == 0, run.output
            assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == []
            assert run.values("ENTRABOT_BLOB_ENDPOINT") == ["https://old.example.invalid"]
            assert run.values("ENTRABOT_BLOB_CONTAINER") == ["old-container"]

        def test_a_new_machine_falls_back_to_local_memory(self, run_steps, failure):
            run = run_steps(use_cloud=True, **failure)
            assert run.exit_code == 0, run.output
            assert run.values("ENTRABOT_KEEP_MEMORY_LOCAL") == ["true"]
            assert run.values("ENTRABOT_BLOB_ENDPOINT") == []
