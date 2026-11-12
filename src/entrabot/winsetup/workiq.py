"""Optional: Microsoft Agent 365 Work IQ (Word, OneDrive/SharePoint, Excel).

Port of ``Configure-A365WorkIq`` from the old ``setup-windows.ps1`` and of
``configure_a365_work_iq`` in ``scripts/setup.sh``. The ``a365`` CLI is only needed when this
step is asked for. Every command runs from the project folder, because ``a365`` and the
manifest check read files relative to the current directory.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from entrabot.winsetup.azcli import AzCli, AzError
from entrabot.winsetup.console import Console
from entrabot.winsetup.helpers import Helpers, Paths

A365_MCP_SERVERS = ("mcp_WordServer", "mcp_ODSPRemoteServer", "mcp_ExcelServer")
A365_INSTALL_HINT = (
    "Work IQ needs the .NET SDK and the Agent 365 CLI. Install the .NET SDK from "
    "https://dotnet.microsoft.com/download, then run:\n"
    "  dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli\n"
    "and run setup again."
)


class WorkIqError(Exception):
    """The Work IQ step failed."""


def find_a365(
    env: Mapping[str, str], *, which: Callable[[str], str | None] = shutil.which
) -> Path | None:
    """The ``a365`` CLI: PATH, then the .NET global tools folder."""
    found = which("a365")
    if found:
        return Path(found)
    home = env.get("USERPROFILE") or env.get("HOME")
    if home:
        for name in ("a365.exe", "a365"):
            candidate = Path(home) / ".dotnet" / "tools" / name
            if candidate.is_file():
                return candidate
    return None


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
    """Point the Agent 365 tooling at the existing Entrabot Blueprint and grant it Work IQ."""
    if not blueprint_app_id:
        raise WorkIqError("The agent must be created before Work IQ can be configured.")
    console.info("Configuring Microsoft Agent 365 Work IQ (Word, OneDrive/SharePoint, Excel).")
    console.detail(
        "A Microsoft sign-in may open. If asked to create the Agent 365 CLI app, choose C."
    )

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
    if console.run(
        [str(a365), "setup", "requirements"], env=env, cwd=cwd, interactive=True
    ).returncode:
        raise WorkIqError("'a365 setup requirements' failed (see above).")

    added = console.run(
        [str(a365), "develop", "add-mcp-servers", *A365_MCP_SERVERS, "--project-path", str(cwd)],
        env=env,
        cwd=cwd,
    )
    if added.returncode != 0:
        raise WorkIqError("'a365 develop add-mcp-servers' failed (see above).")

    ensured = helpers.script(
        "ensure_a365_work_iq_permissions.py", ["--blueprint-app-id", blueprint_app_id]
    )
    if ensured.returncode != 0:
        raise WorkIqError("Granting the Work IQ permissions failed (see above).")

    permissions = console.run([str(a365), "setup", "permissions", "mcp"], env=env, cwd=cwd)
    if permissions.returncode != 0:
        raise WorkIqError("'a365 setup permissions mcp' failed (see above).")
    if "OAuth2 grants failed" in permissions.stdout + permissions.stderr:
        raise WorkIqError(
            "'a365 setup permissions mcp' reported OAuth2 grants failed, so the Work IQ "
            "permissions are incomplete. Resolve the Agent 365 Tools admin-consent issue it "
            "describes, then run setup again."
        )

    if helpers.script("spike_a365_work_iq.py").returncode != 0:
        raise WorkIqError("The Work IQ tooling manifest did not validate (see above).")
    console.ok("Work IQ configured (Word, OneDrive/SharePoint, Excel)")
