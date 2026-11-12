"""Optional: Microsoft Agent 365 Work IQ (Word, OneDrive/SharePoint, Excel).

Port of ``Configure-A365WorkIq`` from the old ``setup-windows.ps1`` and of
``configure_a365_work_iq`` in ``scripts/setup.sh``. The ``a365`` CLI is only needed when this
step is asked for. Every command runs from the project folder, because ``a365`` and the
manifest check read files relative to the current directory.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from entrabot.winsetup.azcli import AzCli, AzError
from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import Helpers, Paths

A365_MCP_SERVERS = ("mcp_WordServer", "mcp_ODSPRemoteServer", "mcp_ExcelServer")
# The repo's helper installs what Work IQ needs without administrator rights: the Agent 365
# CLI, and the .NET SDK it runs on when the PC has none, both under Entrabot's own folder
# (TOOLS_FOLDER); and PowerShell 7 with winget. Where it cannot, it says where to get each.
INSTALL_COMMAND = (
    "powershell -ExecutionPolicy Bypass -File scripts\\prereqs-windows.ps1 -WithWorkIq"
)
A365_INSTALL_HINT = (
    "Word, Excel and OneDrive files (Work IQ) need the .NET SDK, the Microsoft Agent 365 CLI\n"
    "and PowerShell 7. To install them for your account, with no administrator rights, run:\n"
    f"  {INSTALL_COMMAND}\n"
    "then run setup again."
)
# Under the data folder (%LOCALAPPDATA%\entrabot), beside the private Azure CLI.
TOOLS_FOLDER = "tools"
A365_FOLDER = "a365"
DOTNET_FOLDER = "dotnet"
# The variables a .NET apphost reads before DOTNET_ROOT: one of these left set would send
# a365.exe to some other .NET (Microsoft Learn, ".NET environment variables").
_WINNING_DOTNET_ROOTS = (
    "DOTNET_ROOT_X64",
    "DOTNET_ROOT_X86",
    "DOTNET_ROOT_ARM64",
    "DOTNET_ROOT(x86)",
)


# What a365 prints when it could not sign in (MSAL's words, seen on a real run over SSH,
# where the sign-in window cannot open).
_SIGN_IN_FAILED = ("MsalAuthenticationFailedException", "Interactive authentication failed")
SIGN_IN_REASON = "the Agent 365 tool could not sign in to Microsoft"
STEP_REASON = "one of the steps that switch them on did not finish"
FIRST_STEP_REASON = (
    "the Agent 365 tool's first step, which signs in and checks the tenant, did not finish "
    "(its messages are on the screen above, where it ran)"
)
# The files the Work IQ step writes in the project folder. A failed attempt puts them back
# the way they were, so nothing (setup's next run, the agent) takes Work IQ to be on.
_PROJECT_FILES = ("ToolingManifest.json", "a365.config.json")


class WorkIqError(Exception):
    """The Work IQ step failed. ``str(error)`` says which step, for the screen at that point
    and the log; :attr:`reason` says why in plain words, for the last screen; and
    :attr:`signed_in` is False when a365 could not sign in."""

    def __init__(self, message: str, *, reason: str | None = None, signed_in: bool = True):
        super().__init__(message)
        self.reason = reason or message
        self.signed_in = signed_in


def _failed(message: str, output: str) -> WorkIqError:
    """The error for an a365 step that failed and printed *output*."""
    if any(marker in output for marker in _SIGN_IN_FAILED):
        return WorkIqError(message, reason=SIGN_IN_REASON, signed_in=False)
    return WorkIqError(message, reason=STEP_REASON)


def private_a365(data_root: Path) -> Path:
    return data_root / TOOLS_FOLDER / A365_FOLDER / "a365.exe"


def private_dotnet(data_root: Path) -> Path:
    return data_root / TOOLS_FOLDER / DOTNET_FOLDER / "dotnet.exe"


def find_a365(
    env: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    data_root: Path | None = None,
) -> Path | None:
    """The ``a365`` CLI: PATH, then the .NET global tools folder, then Entrabot's own copy
    under *data_root* (the one the helper installs without administrator rights)."""
    found = which("a365")
    if found:
        return Path(found)
    home = env.get("USERPROFILE") or env.get("HOME")
    if home:
        for name in ("a365.exe", "a365"):
            candidate = Path(home) / ".dotnet" / "tools" / name
            if candidate.is_file():
                return candidate
    if data_root is not None and private_a365(data_root).is_file():
        return private_a365(data_root)
    return None


def find_pwsh(
    env: Mapping[str, str], *, which: Callable[[str], str | None] = shutil.which
) -> Path | None:
    """PowerShell 7, which one of the ``a365`` steps needs: PATH, then where its installers
    put it (the MSI under Program Files, the MSIX's alias under WindowsApps). Just installed,
    it is not on this process's PATH yet."""
    found = which("pwsh")
    if found:
        return Path(found)
    places = (
        (env.get("ProgramFiles") or env.get("PROGRAMFILES"), ("PowerShell", "7", "pwsh.exe")),
        (env.get("LOCALAPPDATA"), ("Microsoft", "WindowsApps", "pwsh.exe")),
    )
    for base, parts in places:
        if base and Path(base).joinpath(*parts).is_file():
            return Path(base).joinpath(*parts)
    return None


@dataclass(frozen=True)
class WorkIqTools:
    a365: Path | None = None
    pwsh: Path | None = None
    # Entrabot's own .NET, when the a365 found is Entrabot's own copy and runs on it. None
    # otherwise: an a365 installed with the PC's own .NET finds it by itself.
    dotnet_root: Path | None = None

    @property
    def missing(self) -> list[str]:
        """What is not installed, as a person would say it."""
        named = ((self.a365, "the Microsoft Agent 365 CLI"), (self.pwsh, "PowerShell 7"))
        return [name for found, name in named if found is None]


def find_tools(
    env: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    data_root: Path | None = None,
) -> WorkIqTools:
    a365 = find_a365(env, which=which, data_root=data_root)
    dotnet_root = None
    if data_root is not None and a365 == private_a365(data_root):
        dotnet = private_dotnet(data_root)
        dotnet_root = dotnet.parent if dotnet.is_file() else None
    return WorkIqTools(a365=a365, pwsh=find_pwsh(env, which=which), dotnet_root=dotnet_root)


def use_tools(
    env: MutableMapping[str, str],
    tools: WorkIqTools,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> None:
    """Make *env*, which the ``a365`` steps run with, find *tools*: the folders of tools
    found off PATH go at the front of PATH (a365 starts PowerShell 7 by name, and may start
    dotnet), and with Entrabot's own .NET, DOTNET_ROOT names it, which a365.exe needs to find
    a runtime that is not installed for the whole PC."""
    folders = [] if tools.dotnet_root is None else [str(tools.dotnet_root)]
    folders += [
        str(tool.parent)
        for tool, name in ((tools.pwsh, "pwsh"), (tools.a365, "a365"))
        if tool is not None and not which(name)
    ]
    current = env.get("PATH", "")
    known = {part.lower() for part in current.split(os.pathsep) if part}
    added = [folder for folder in dict.fromkeys(folders) if folder.lower() not in known]
    if added:
        env["PATH"] = os.pathsep.join([*added, current]) if current else os.pathsep.join(added)
    if tools.dotnet_root is not None:
        env["DOTNET_ROOT"] = str(tools.dotnet_root)
        for name in _WINNING_DOTNET_ROOTS:
            env.pop(name, None)


def _write_config(
    paths: Paths,
    az: AzCli,
    *,
    tenant_id: str,
    blueprint_app_id: str,
    blueprint_object_id: str,
    agent_id: str,
) -> None:
    try:
        apps = az.json(["ad", "app", "list", "--display-name", "Agent 365 CLI"])
        principal = az.json(["ad", "sp", "show", "--id", agent_id])
    except AzError as exc:
        raise WorkIqError(f"Could not read the tenant's app registrations: {exc}") from exc
    client_app_id = apps[0].get("appId") if isinstance(apps, list) and apps else None
    if not client_app_id:
        raise WorkIqError(
            "The 'Agent 365 CLI' app registration was not found in this tenant. Run "
            "'a365 setup requirements' once and choose C to create it, then run setup again."
        )
    display_name = principal.get("displayName") if isinstance(principal, dict) else None
    if not display_name:
        raise WorkIqError(f"Could not read the Agent Identity's name ({agent_id}).")

    config_path = paths.project_root / "a365.config.json"
    config: dict[str, object] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise WorkIqError(f"{config_path.name} is not valid JSON: {exc}") from exc
        if isinstance(loaded, dict):
            config = loaded
    config.update(
        {
            "tenantId": tenant_id,
            "clientAppId": client_app_id,
            "agentBlueprintId": blueprint_app_id,
            "agentBlueprintDisplayName": "EntraBot Code Agent",
            "agentIdentityDisplayName": display_name,
            "deploymentProjectPath": str(paths.project_root),
        }
    )
    if blueprint_object_id:
        config["agentBlueprintObjectId"] = blueprint_object_id
    if agent_id:
        config["agentIdentityId"] = agent_id
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def word_tools_configured(project_root: Path) -> bool:
    """Has an earlier run given this agent its Word tools? Read from the tooling manifest
    that ``a365 develop add-mcp-servers`` fills in. Anything unreadable counts as no: the
    answer only decides which suggestions setup's last screen makes."""
    manifest = project_root / "ToolingManifest.json"
    try:
        return A365_MCP_SERVERS[0] in manifest.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False


def configure_work_iq(
    *,
    paths: Paths,
    helpers: Helpers,
    console: Console,
    az: AzCli,
    a365: Path,
    tenant_id: str,
    blueprint_app_id: str,
    blueprint_object_id: str,
    agent_id: str,
) -> None:
    """Point the Agent 365 tooling at the existing Entrabot Blueprint and grant it Work IQ.
    One step opens a Microsoft sign-in window, so this needs the PC's own desktop. A failure
    raises :class:`WorkIqError` and leaves the project's Work IQ files as they were."""
    if not blueprint_app_id:
        raise WorkIqError("The agent must be created before Work IQ can be configured.")
    saved = {name: _saved(paths.project_root / name) for name in _PROJECT_FILES}
    try:
        _configure(
            paths=paths,
            helpers=helpers,
            console=console,
            az=az,
            a365=a365,
            tenant_id=tenant_id,
            blueprint_app_id=blueprint_app_id,
            blueprint_object_id=blueprint_object_id,
            agent_id=agent_id,
        )
    except BaseException:  # a failure, or Ctrl+C: either way it did not finish
        for name, content in saved.items():
            _put_back(paths.project_root / name, content, console)
        raise


def _saved(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _put_back(path: Path, content: bytes | None, console: Console) -> None:
    try:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
    except OSError as exc:
        console.warn(f"Could not put {path.name} back the way it was: {exc}")


def _configure(
    *,
    paths: Paths,
    helpers: Helpers,
    console: Console,
    az: AzCli,
    a365: Path,
    tenant_id: str,
    blueprint_app_id: str,
    blueprint_object_id: str,
    agent_id: str,
) -> None:
    console.info("Configuring Microsoft Agent 365 Work IQ (Word, OneDrive/SharePoint, Excel).")
    console.detail(
        "Microsoft's Agent 365 tool signs in through Windows, in a window of its own, and may"
    )
    console.detail("ask more than once. That is separate from the sign-in in your browser.")
    console.detail("If asked to create the Agent 365 CLI app, choose C.")

    manifest = paths.project_root / "ToolingManifest.json"
    if not manifest.is_file():
        manifest.write_text('{"mcpServers":[]}\n', encoding="utf-8")

    _write_config(
        paths,
        az,
        tenant_id=tenant_id,
        blueprint_app_id=blueprint_app_id,
        blueprint_object_id=blueprint_object_id,
        agent_id=agent_id,
    )

    cwd, env = paths.project_root, helpers.base_env
    # In the person's terminal: it may ask before changing the app registration. So its
    # output is theirs to read, and setup cannot see it.
    if console.run(
        [str(a365), "setup", "requirements"], env=env, cwd=cwd, interactive=True
    ).returncode:
        raise WorkIqError("'a365 setup requirements' failed (see above).", reason=FIRST_STEP_REASON)

    added = console.run(
        [str(a365), "develop", "add-mcp-servers", *A365_MCP_SERVERS, "--project-path", str(cwd)],
        env=env,
        cwd=cwd,
    )
    if added.returncode != 0:
        raise _failed(
            "'a365 develop add-mcp-servers' failed (see above).", added.stdout + added.stderr
        )

    ensured = helpers.script(
        "ensure_a365_work_iq_permissions.py", ["--blueprint-app-id", blueprint_app_id]
    )
    if ensured.returncode != 0:
        raise WorkIqError(
            "Granting the Work IQ permissions failed (see above).", reason=STEP_REASON
        )

    permissions = console.run([str(a365), "setup", "permissions", "mcp"], env=env, cwd=cwd)
    output = permissions.stdout + permissions.stderr
    if permissions.returncode != 0:
        raise _failed("'a365 setup permissions mcp' failed (see above).", output)
    if "OAuth2 grants failed" in output:
        raise WorkIqError(
            "'a365 setup permissions mcp' reported OAuth2 grants failed, so the Work IQ "
            "permissions are incomplete. Resolve the Agent 365 Tools admin-consent issue it "
            "describes, then run setup again.",
            reason="the Agent 365 tool could not grant all of their permissions",
        )

    if helpers.script("spike_a365_work_iq.py").returncode != 0:
        raise WorkIqError(
            "The Work IQ tooling manifest did not validate (see above).", reason=STEP_REASON
        )
    console.ok("Work IQ configured (Word, OneDrive/SharePoint, Excel)")
