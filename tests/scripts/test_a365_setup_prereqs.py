from __future__ import annotations

from pathlib import Path

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
    # Opt-in: none of it runs unless -WithWorkIq is passed.
    assert "[switch]$WithWorkIq" in script
    assert script.index("if ($WithWorkIq) {") < script.index("Microsoft.DotNet.SDK.9")


def test_windows_setup_does_not_require_a365_cli_unless_work_iq_is_requested() -> None:
    # The a365 CLI (and the .NET SDK behind it) used to be a hard prerequisite of every
    # Windows setup, so a blocked NuGet feed blocked setup for people who never wanted
    # Work IQ. The bootstrap no longer probes for it; it only forwards the request.
    script = read_script("scripts/setup-windows.ps1")

    assert "Found: python, az, git, pwsh, a365" not in script
    assert "Get-Command a365" not in script
    assert "'--configure-a365-work-iq'" in script
    assert "scripts\\prereqs-windows.ps1 -WithWorkIq" in script


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
    assert (
        "A365_WORK_IQ_MCP_SERVERS=(mcp_WordServer mcp_ODSPRemoteServer mcp_ExcelServer)"
        in script
    )
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
    assert 'success "A365 Work IQ Word and Excel manifest configured"' in script
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

    assert "ENTRABOT_AGENT_USER_UPN" in script
    assert 'explicit_upn = os.environ.get("ENTRABOT_AGENT_USER_UPN", "").strip()' in script
    assert "Using explicit Agent User UPN" in script


def test_unix_setup_preflights_copilot_license_for_work_iq() -> None:
    script = read_script("scripts/setup.sh")

    assert "check_copilot_license_availability" in script
    assert "Copilot license available" in script
    assert 'if [ "$WITH_A365_WORK_IQ" = true ]; then' in script
    assert "ENTRABOT_ASSIGN_WORK_IQ_LICENSE=1" in script


def test_windows_setup_forwards_work_iq_configuration_to_the_wizard() -> None:
    # Work IQ configuration itself (license assignment, a365 CLI calls and their order)
    # moved out of PowerShell into the Python wizard, entrabot.winsetup, where it is unit
    # tested. The bootstrap's whole job is to pass the request through.
    script = read_script("scripts/setup-windows.ps1")

    assert "[switch]$ConfigureA365WorkIq" in script
    assert '[string]$AgentUserUpn = ""' in script
    assert (
        "if (Test-BoundSwitch $Bound 'ConfigureA365WorkIq') "
        "{ $list.Add('--configure-a365-work-iq') }"
    ) in script
    assert "a365 setup" not in script
    assert "ENTRABOT_ASSIGN_WORK_IQ_LICENSE" not in script


def test_windows_setup_keeps_accepting_the_deprecated_a365_agent_name() -> None:
    script = read_script("scripts/setup-windows.ps1")

    assert "[string]$A365AgentName" in script
    assert "-A365AgentName is deprecated and ignored" in script
