from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read_script(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8-sig")


def test_windows_prereqs_installs_dotnet_and_a365_cli_for_the_account_only() -> None:
    # From a real run as a standard user: winget's .NET SDK installer needs elevation (it
    # exited 1459), so Work IQ stayed off. The SDK, when the PC has none, and the a365 CLI now
    # go under %LOCALAPPDATA%\entrabot\tools, with no winget and no administrator rights.
    script = read_script("scripts/prereqs-windows.ps1")

    assert "Microsoft.DotNet.SDK" not in script
    assert "https://dot.net/v1/dotnet-install.ps1" in script
    assert "Microsoft.Agents.A365.DevTools.Cli" in script
    assert "'--tool-path'" in script
    assert "dotnet tool install --global" not in script
    # An a365 the person installed as a global tool is still kept up to date.
    assert "dotnet tool update --global $A365Package" in script
    # Opt-in: none of it runs unless -WithWorkIq is passed.
    assert "[switch]$WithWorkIq" in script
    assert script.index("if ($WithWorkIq) {") < script.index("Install-PrivateDotnet -Folder")


def test_winget_runs_attached_to_the_window_so_its_progress_bar_draws_in_place() -> None:
    # From a desktop run: winget's output went through a PowerShell pipe, which printed every
    # redraw of its progress bar as a new line of mis-decoded characters. winget now runs
    # attached to the window, and its exit code is read from the process.
    script = read_script("scripts/prereqs-windows.ps1")
    body = script[script.index("function Install-WithWinget {") :]
    body = body[: body.index("\n}")]

    assert "| Out-Host" not in body
    for word in ("Start-Process", "-NoNewWindow", "-Wait", "-PassThru"):
        assert word in body
    assert "Test-WingetSuccess -ExitCode $process.ExitCode" in body
    # An argument with spaces (the build tools' --override) still arrives as one argument.
    assert "-match '\\s'" in body


def test_windows_setup_does_not_require_a365_cli_unless_work_iq_is_requested() -> None:
    # The a365 CLI (and the .NET SDK behind it) used to be a hard prerequisite of every
    # Windows setup, so a blocked NuGet feed blocked setup for people who never wanted
    # Work IQ. The bootstrap no longer probes for it; it only forwards the request.
    script = read_script("scripts/setup-windows.ps1")

    assert "Found: python, az, git, pwsh, a365" not in script
    assert "Get-Command a365" not in script
    assert "'--configure-a365-work-iq'" in script
    assert "scripts\\prereqs-windows.ps1 -WithWorkIq" in script


def test_unix_setup_switches_work_iq_on_without_the_a365_cli() -> None:
    # setup.sh used to install Microsoft's Agent 365 CLI (through a .NET SDK), need
    # PowerShell 7, write a365.config.json and run three a365 commands. It now starts the
    # setup wizard, which writes the manifest from the package's catalog, runs
    # ensure_a365_work_iq_permissions.py and checks the agent's token scopes itself
    # (entrabot.winsetup.workiq), exactly as on Windows. Both flags ask for that.
    script = read_script("scripts/setup.sh")

    assert "--configure-a365-work-iq)    ARGS+=(--configure-a365-work-iq)" in script
    assert "--with-a365-work-iq)" in script and "--a365-agent-name=*)" in script
    for gone in (
        "dotnet tool",
        "Microsoft.Agents.A365.DevTools.Cli",
        "pwsh",
        "a365 setup",
        "a365 develop",
        "a365.config.json",
        "ENTRABOT_ASSIGN_WORK_IQ_LICENSE",
    ):
        assert gone not in script, gone
    wizard = read_script("src/entrabot/winsetup/workiq.py")
    assert 'PERMISSIONS_SCRIPT = "ensure_a365_work_iq_permissions.py"' in wizard


def test_unix_setup_passes_identity_flags_to_the_wizard() -> None:
    # --new with an explicit Agent User UPN, or a suffix: the wizard hands them to
    # create_entra_agent_ids.py (entrabot.winsetup.state.identity_environment).
    script = read_script("scripts/setup.sh")
    state = read_script("src/entrabot/winsetup/state.py")

    assert '--new)                       ARGS+=(--new-chain)' in script
    assert '--agent-user-upn=*)          ARGS+=(--agent-user-upn "$(value_of "$arg")")' in script
    assert '--with-upn-suffix=*)         ARGS+=(--upn-suffix "$(value_of "$arg")")' in script
    assert "ENTRABOT_AGENT_USER_UPN" in state and "_ENTRABOT_UPN_SUFFIX" in state


def test_macos_prereqs_no_longer_install_the_a365_cli_unless_asked() -> None:
    script = read_script("scripts/prereqs-macos.sh")

    assert "SKIP_A365=true\nSKIP_PWSH=true\n" in script  # the default
    assert "--with-a365-cli) SKIP_A365=false; SKIP_PWSH=false ;;" in script
    assert "--core-only) SKIP_A365=true; SKIP_PWSH=true ;;" in script  # still accepted


def test_unix_teardown_supports_targeted_upn_and_preserves_cloud_storage() -> None:
    script = read_script("scripts/teardown.sh")

    assert "--agent-user-upn=*" in script
    assert "--dry-run" in script
    assert "deprovision_entra_agent_identity.py" in script
    assert "Cloud storage is not deleted by teardown.sh" in script
    assert "targeted Agent Identity teardown cannot proceed" not in script
    assert "az storage account delete" not in script
    assert "az storage container delete" not in script


def test_create_entra_agent_ids_allows_explicit_agent_user_upn() -> None:
    script = read_script("scripts/create_entra_agent_ids.py")

    assert "ENTRABOT_AGENT_USER_UPN" in script
    assert 'explicit_upn = os.environ.get("ENTRABOT_AGENT_USER_UPN", "").strip()' in script
    assert "Using explicit Agent User UPN" in script


def test_the_wizard_asks_for_a_copilot_licence_only_when_work_iq_is_asked_for() -> None:
    # What setup.sh did with ENTRABOT_ASSIGN_WORK_IQ_LICENSE and its Copilot preflight is the
    # wizard's on every system now.
    wizard = read_script("src/entrabot/winsetup/wizard.py")

    assert 'env.pop("ENTRABOT_ASSIGN_WORK_IQ_LICENSE", None)' in wizard
    assert 'env["ENTRABOT_ASSIGN_WORK_IQ_LICENSE"] = "1"' in wizard
    assert "check_copilot_license_availability" in read_script("src/entrabot/winsetup/helpers.py")


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
