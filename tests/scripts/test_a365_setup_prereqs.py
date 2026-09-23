from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def read_script(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8-sig")


def test_windows_prereqs_installs_dotnet_and_a365_cli() -> None:
    script = read_script("scripts/prereqs-windows.ps1")

    assert "Microsoft.DotNet.SDK.9" in script
    assert "Microsoft.Agents.A365.DevTools.Cli" in script
    assert "dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli" in script
    assert "dotnet tool update --global Microsoft.Agents.A365.DevTools.Cli" in script
    assert "a365" in script


def test_windows_setup_probes_a365_cli() -> None:
    script = read_script("scripts/setup-windows.ps1")

    assert "a365" in script
    assert "Found: python, az, git, pwsh, a365" in script
    assert "scripts\\prereqs-windows.ps1" in script


def test_windows_setup_uploads_blueprint_certificate_before_writing_env() -> None:
    script = read_script("scripts/setup-windows.ps1")

    assert "function Read-EnvValue" in script
    assert "'verify_blueprint_cert.py'" in script
    assert "GetRawCertData()" in script
    assert "Reusing registered local Blueprint certificate" in script
    assert "Repairing Blueprint registration with existing local certificate" in script
    generate = script.index("'generate_windows_cert.py'")
    upload = script.index("'upload_blueprint_cert.py'", generate)
    write_env = script.index("Update-EnvFile $envPath", upload)
    assert generate < upload < write_env


@pytest.mark.skipif(sys.platform != "win32", reason="Windows setup script")
def test_windows_update_env_file_writes_one_key_per_line_for_new_file(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    env_path = tmp_path / ".env"
    probe = tmp_path / "probe.ps1"
    # Load only Update-EnvFile from setup; nothing else in the script runs.
    probe.write_text(
        r"""
param([string]$SetupScript, [string]$EnvPath)
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $SetupScript, [ref]$null, [ref]$null
)
$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Update-EnvFile'
}, $true)
. ([scriptblock]::Create($function.Extent.Text))
Update-EnvFile $EnvPath @{ FIRST_KEY = 'one'; SECOND_KEY = 'two'; THIRD_KEY = 'three' }
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            pwsh, "-NoProfile", "-NonInteractive", "-File", str(probe),
            str(REPO_ROOT / "scripts" / "setup-windows.ps1"), str(env_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    assert sorted(lines) == ["FIRST_KEY=one", "SECOND_KEY=two", "THIRD_KEY=three"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native argument passing")
@pytest.mark.parametrize(
    ("helper_name", "expected_arguments"),
    [
        (
            "upload_blueprint_cert.py",
            ["--blueprint-object-id", "fixture-blueprint-object", "--der-path"],
        ),
    ],
)
def test_windows_setup_passes_separate_native_arguments(
    tmp_path: Path, helper_name: str, expected_arguments: list[str]
) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    scripts = tmp_path / "fixture scripts"
    scripts.mkdir()
    (scripts / helper_name).write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
    )
    probe = tmp_path / "probe.ps1"
    # Execute only the native invocation from setup, against an argv recorder. No provisioning,
    # certificate-store access, or imports of Entrabot's live configuration can run.
    probe.write_text(
        r"""
param([string]$SetupScript, [string]$VenvPython, [string]$ScriptDir, [string]$HelperName)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$BlueprintObjectId = 'fixture-blueprint-object'
$BlueprintAppId = 'fixture-blueprint-app'
$derPath = Join-Path $ScriptDir 'fixture cert.cer'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $SetupScript, [ref]$null, [ref]$parseErrors
)
if ($parseErrors.Count) { throw 'Setup script has parse errors' }
$commands = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
    $node.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand -and
    $node.CommandElements[0].Extent.Text -eq '$VenvPython' -and
    $node.Extent.Text.Contains("'$HelperName'")
}, $true))
if ($commands.Count -ne 1) { throw 'Expected exactly one helper invocation' }
& ([scriptblock]::Create($commands[0].Extent.Text))
exit $LASTEXITCODE
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(probe),
            str(REPO_ROOT / "scripts" / "setup-windows.ps1"),
            sys.executable,
            str(scripts),
            helper_name,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if helper_name == "upload_blueprint_cert.py":
        expected_arguments = [*expected_arguments, str(scripts / "fixture cert.cer")]
    assert json.loads(result.stdout) == expected_arguments


def test_unix_setup_can_install_a365_cli_when_requested() -> None:
    script = read_script("scripts/setup.sh")

    assert "--with-a365-work-iq" in script
    assert "WITH_A365_WORK_IQ=false" in script
    assert "dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli" in script
    assert "dotnet tool update --global Microsoft.Agents.A365.DevTools.Cli" in script
    assert "--agent-user-upn=*" in script
    assert 'export ENTRABOT_AGENT_USER_UPN="$AGENT_USER_UPN"' in script
    assert 'export _ENTRABOT_UPN_SUFFIX="$UPN_SUFFIX"' in script


def test_unix_setup_can_create_new_chain_with_explicit_agent_user_upn() -> None:
    script = read_script("scripts/setup.sh")

    assert "--new --agent-user-upn=entrabot-agent@yourtenant.onmicrosoft.com" in script
    new_branch = script[script.index('if [ "$NEW_CHAIN" = true ]') :]
    assert 'if [ -n "$AGENT_USER_UPN" ]; then' in new_branch
    assert 'export ENTRABOT_AGENT_USER_UPN="$AGENT_USER_UPN"' in new_branch
    assert 'elif [ -z "$UPN_SUFFIX" ]; then' in new_branch


def test_unix_teardown_supports_targeted_upn_and_preserves_cloud_storage() -> None:
    script = read_script("scripts/teardown.sh")

    assert "--agent-user-upn=*" in script
    assert "--dry-run" in script
    assert "deprovision_entra_agent_identity.py" in script
    assert "Cloud storage is not deleted by teardown.sh" in script
    assert "targeted Agent Identity teardown cannot proceed" not in script
    assert "az storage account delete" not in script
    assert "az storage container delete" not in script


def test_unix_setup_can_run_interactive_a365_work_iq_configuration() -> None:
    script = read_script("scripts/setup.sh")

    assert "--configure-a365-work-iq" in script
    assert "CONFIGURE_A365_WORK_IQ=false" in script
    assert 'if [ "$CONFIGURE_A365_WORK_IQ" = true ] && ! command -v pwsh' in script
    assert "PowerShell 7+ (pwsh)" in script
    assert "brew install powershell" in script
    assert "brew install --cask powershell" not in script
    assert 'A365_AGENT_NAME="EntraBot Code Agent"' in script
    assert "--a365-agent-name=*" in script
    assert "ensure_a365_tooling_manifest" in script
    assert 'printf \'{"mcpServers":[]}' in script
    assert "write_a365_config" in script
    assert 'az ad sp show --id "$AGENT_ID" --query displayName -o tsv' in script
    assert '"agentBlueprintId": blueprint_app_id' in script
    assert '"agentIdentityDisplayName": agent_identity_display_name' in script
    assert '"deploymentProjectPath": str(project_root)' in script
    assert "find_local_blueprint_cert.py" in script
    assert 'A365_WORK_IQ_MCP_SERVERS=(mcp_WordServer mcp_ODSPRemoteServer)' in script
    assert (
        'a365 develop add-mcp-servers "${A365_WORK_IQ_MCP_SERVERS[@]}" '
        '--project-path "$PROJECT_ROOT"'
        in script
    )
    assert "a365 setup requirements" in script
    assert 'a365 setup blueprint --agent-name "$A365_AGENT_NAME"' not in script
    assert 'a365 setup permissions mcp --agent-name "$A365_AGENT_NAME"' not in script
    assert "a365 setup permissions mcp" in script
    assert "A365_PERMISSIONS_LOG=" in script
    assert "OAuth2 grants failed" in script
    assert "ensure_a365_work_iq_permissions.py" in script
    assert '"$SCRIPT_PYTHON" "$PROJECT_ROOT/scripts/ensure_a365_work_iq_permissions.py"' in script
    assert '"$SCRIPT_PYTHON" "$PROJECT_ROOT/scripts/spike_a365_work_iq.py"' in script
    assert '--blueprint-app-id "$BLUEPRINT_APP_ID"' in script
    config_call = script.index("write_a365_config")
    requirements_call = script.index("a365 setup requirements", config_call)
    preflight_call = script.index("ensure_a365_work_iq_permissions.py", config_call)
    permissions_call = script.index("a365 setup permissions mcp", config_call)
    assert config_call < requirements_call < preflight_call < permissions_call
    assert script.index('success "Agent User:') < script.rindex(
        'if [ "$CONFIGURE_A365_WORK_IQ" = true ]'
    )
    assert "scripts/spike_a365_work_iq.py" in script
    assert 'fail "A365 Work IQ manifest validation failed"' in script


def test_create_entra_agent_ids_allows_explicit_agent_user_upn() -> None:
    script = read_script("scripts/create_entra_agent_ids.py")

    capture = script.index(
        '_EXPLICIT_AGENT_USER_UPN = os.environ.get("ENTRABOT_AGENT_USER_UPN", "").strip()'
    )
    config_loading_import = script.index("from entrabot.preflight import")
    assert capture < config_loading_import
    assert "explicit_upn = _EXPLICIT_AGENT_USER_UPN" in script
    assert "Using explicit Agent User UPN" in script


def test_unix_setup_preflights_copilot_license_for_work_iq() -> None:
    script = read_script("scripts/setup.sh")

    assert "check_copilot_license_availability" in script
    assert "Copilot license available" in script
    assert 'if [ "$WITH_A365_WORK_IQ" = true ]; then' in script
    assert "ENTRABOT_ASSIGN_WORK_IQ_LICENSE=1" in script


def test_windows_setup_assigns_work_iq_license_only_for_a365_configuration() -> None:
    script = read_script("scripts/setup-windows.ps1")

    assert "ENTRABOT_ASSIGN_WORK_IQ_LICENSE" in script
    assert "if ($ConfigureA365WorkIq)" in script


def test_windows_setup_pins_active_tenant_and_enables_new_chain_mode() -> None:
    script = read_script("scripts/setup-windows.ps1")

    account_check = script.index("$account = az account show")
    tenant_export = script.index("$env:ENTRABOT_TENANT_ID = $account.tenantId", account_check)
    provisioner_call = script.index("'entra_provisioning.py'", tenant_export)
    assert account_check < tenant_export < provisioner_call
    assert "Remove-Item Env:ENTRABOT_NEW_CHAIN -ErrorAction SilentlyContinue" in script
    assert "Remove-Item Env:ENTRABOT_AGENT_USER_UPN -ErrorAction SilentlyContinue" in script
    assert "Remove-Item Env:_ENTRABOT_UPN_SUFFIX -ErrorAction SilentlyContinue" in script
    assert "if ($NewChain) {" in script
    assert "$env:ENTRABOT_NEW_CHAIN = '1'" in script
    assert "$env:_ENTRABOT_UPN_SUFFIX = $UpnSuffix" in script
    assert "$env:_ENTRABOT_USE_BLUEPRINT = $UseBlueprint" in script


def test_unix_setup_installs_local_package_before_importing_provisioning() -> None:
    script = read_script("scripts/setup.sh")
    install = script.index('pip install --quiet -e "$PROJECT_ROOT[dev,provisioning]"')
    assert 'SCRIPT_PYTHON="$PROJECT_ROOT/.venv/bin/python3"' in script[:install]
    assert install < script.index('"$SCRIPT_DIR/entra_provisioning.py"')
    assert 'export ENTRABOT_TENANT_ID="$TENANT_ID"' in script
    assert 'export _ENTRABOT_USE_BLUEPRINT="$USE_BLUEPRINT"' in script


def test_unix_setup_bootstraps_environment_and_dependencies_once() -> None:
    script = read_script("scripts/setup.sh")
    assert script.count("-m venv ") == 1
    assert script.count("pip install --quiet -e ") == 1
    assert script.count("pip setuptools wheel") == 1
    assert 'VENV_PY="$SCRIPT_PYTHON"' in script


@pytest.mark.parametrize("path,provisioner_call", [
    ("scripts/setup.sh", '"$SCRIPT_DIR/entra_provisioning.py"'),
    ("scripts/setup-windows.ps1", "'entra_provisioning.py'"),
])
def test_setup_rejects_config_conflicts_before_live_provisioning(path, provisioner_call):
    script = read_script(path)
    assert script.index("validate_setup_context") < script.index(provisioner_call)


def test_windows_setup_can_run_interactive_a365_work_iq_configuration() -> None:
    script = read_script("scripts/setup-windows.ps1")

    assert "[switch]$ConfigureA365WorkIq" in script
    assert '[string]$A365AgentName = "EntraBot Code Agent"' in script
    assert '[string]$AgentUserUpn = ""' in script
    assert "Ensure-A365ToolingManifest" in script
    assert "'{\"mcpServers\":[]}'" in script
    assert "Write-A365Config" in script
    assert 'az ad sp show --id $AgentId --query displayName -o tsv' in script
    assert '$config["agentBlueprintId"] = $BlueprintAppId' in script
    assert '$config["agentIdentityDisplayName"] = $AgentIdentityDisplayName' in script
    assert '$config["deploymentProjectPath"] = $ProjectRoot' in script
    assert '$A365WorkIqMcpServers = @("mcp_WordServer", "mcp_ODSPRemoteServer")' in script
    assert (
        "a365 develop add-mcp-servers $A365WorkIqMcpServers "
        "--project-path $ProjectRoot"
        in script
    )
    assert "a365 setup requirements" in script
    assert "a365 setup blueprint --agent-name $A365AgentName" not in script
    assert "a365 setup permissions mcp --agent-name $A365AgentName" not in script
    assert "a365 setup permissions mcp" in script
    assert "$permissionsOutput = a365 setup permissions mcp 2>&1" in script
    assert "OAuth2 grants failed" in script
    assert "ensure_a365_work_iq_permissions.py" in script
    assert "'--blueprint-app-id', $BlueprintAppId" in script
    config_call = script.index("Write-A365Config")
    requirements_call = script.index("a365 setup requirements", config_call)
    preflight_call = script.index("ensure_a365_work_iq_permissions.py", config_call)
    permissions_call = script.index("a365 setup permissions mcp", config_call)
    assert config_call < requirements_call < preflight_call < permissions_call
    assert script.index("Step 5 \"Provisioning Entra Agent Identity\"") < script.index(
        "if ($ConfigureA365WorkIq)"
    )
    assert "spike_a365_work_iq.py" in script
