"""Known Microsoft Agent 365 Work IQ server catalog."""

from __future__ import annotations

from dataclasses import dataclass

from entrabot.a365.errors import A365ServerNotConfiguredError

AGENT365_SERVER_BASE = "https://agent365.svc.cloud.microsoft/agents/servers"

WORD_SERVER_NAME = "mcp_WordServer"
EXCEL_SERVER_NAME = "mcp_ExcelServer"
MAIL_SERVER_NAME = "mcp_MailTools"
CALENDAR_SERVER_NAME = "mcp_CalendarTools"
ODSP_SERVER_NAME = "mcp_ODSPRemoteServer"
SHAREPOINT_SERVER_NAME = "mcp_SharePointRemoteServer"
ONEDRIVE_SERVER_NAME = "mcp_OneDriveRemoteServer"
USER_SERVER_NAME = "mcp_UserTools"
COPILOT_SERVER_NAME = "mcp_M365Copilot"
DATAVERSE_SERVER_NAME = "mcp_DataverseTools"
TEAMS_SERVER_NAME = "mcp_TeamsServer"

# The resource apps the agent asks tokens for (the manifest's "audience"): the Agent 365 Tools
# app serves the OneDrive/SharePoint and Excel servers, and Word has an app of its own.
AGENT_365_TOOLS_APP_ID = "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1"
WORK_IQ_WORD_APP_ID = "c2d0c2b6-8013-4346-9f8b-b81d3b754a29"
PUBLISHER = "Microsoft"


@dataclass(frozen=True)
class WorkIqServer:
    """Static metadata for one Work IQ MCP server."""

    server_name: str
    display_name: str
    scope: str
    audience: str
    default_endpoint: str
    entrabot_enabled: bool = True


def _server(
    server_name: str, display_name: str, scope: str, enabled: bool = True, audience: str = ""
) -> WorkIqServer:
    return WorkIqServer(
        server_name=server_name,
        display_name=display_name,
        scope=scope,
        audience=audience,
        default_endpoint=f"{AGENT365_SERVER_BASE}/{server_name}",
        entrabot_enabled=enabled,
    )


_SERVERS: dict[str, WorkIqServer] = {
    # Word, Excel and OneDrive/SharePoint: the scope and audience Microsoft's Agent 365 tool
    # writes into ToolingManifest.json for a working agent. Setup writes the same.
    WORD_SERVER_NAME: _server(
        WORD_SERVER_NAME,
        "Work IQ Word",
        "Tools.ListInvoke.All",
        audience=WORK_IQ_WORD_APP_ID,
    ),
    EXCEL_SERVER_NAME: _server(
        EXCEL_SERVER_NAME,
        "Work IQ Excel",
        "McpServers.Excel.All",
        audience=AGENT_365_TOOLS_APP_ID,
    ),
    MAIL_SERVER_NAME: _server(MAIL_SERVER_NAME, "Work IQ Mail", "McpServers.Mail.All"),
    CALENDAR_SERVER_NAME: _server(
        CALENDAR_SERVER_NAME,
        "Work IQ Calendar",
        "Tools.ListInvoke.All",
    ),
    ODSP_SERVER_NAME: _server(
        ODSP_SERVER_NAME,
        "Work IQ OneDrive/SharePoint",
        "McpServers.OneDriveSharepoint.All",
        audience=AGENT_365_TOOLS_APP_ID,
    ),
    SHAREPOINT_SERVER_NAME: _server(
        SHAREPOINT_SERVER_NAME,
        "Work IQ SharePoint",
        "Tools.ListInvoke.All",
    ),
    ONEDRIVE_SERVER_NAME: _server(
        ONEDRIVE_SERVER_NAME,
        "Work IQ OneDrive",
        "Tools.ListInvoke.All",
    ),
    USER_SERVER_NAME: _server(USER_SERVER_NAME, "Work IQ User", "McpServers.User.All"),
    COPILOT_SERVER_NAME: _server(
        COPILOT_SERVER_NAME,
        "Work IQ Microsoft 365 Copilot",
        "Tools.ListInvoke.All",
    ),
    DATAVERSE_SERVER_NAME: _server(
        DATAVERSE_SERVER_NAME,
        "Work IQ Dataverse",
        "McpServers.Dataverse.All",
    ),
    TEAMS_SERVER_NAME: _server(
        TEAMS_SERVER_NAME,
        "Work IQ Teams",
        "McpServers.Teams.All",
        enabled=False,
    ),
}


def get_server(server_name: str) -> WorkIqServer:
    """Return metadata for a known Work IQ server."""
    try:
        return _SERVERS[server_name]
    except KeyError as exc:
        raise A365ServerNotConfiguredError(server_name) from exc


def list_servers() -> list[WorkIqServer]:
    """Return all known Work IQ servers as a copy."""
    return list(_SERVERS.values())


# The servers setup switches on for Word, Excel and OneDrive files, in the order it writes them.
SETUP_SERVER_NAMES = (WORD_SERVER_NAME, ODSP_SERVER_NAME, EXCEL_SERVER_NAME)
# Granted to the Blueprint beyond the servers' own scopes, as Microsoft's tool grants it: the
# metadata scope of the Agent 365 Tools app.
_EXTRA_GRANTED_SCOPES: dict[str, tuple[str, ...]] = {
    AGENT_365_TOOLS_APP_ID: ("McpServersMetadata.Read.All",),
}
# The order the resource apps are granted in.
_RESOURCE_ORDER = (AGENT_365_TOOLS_APP_ID, WORK_IQ_WORD_APP_ID)


def setup_servers() -> list[WorkIqServer]:
    """The servers setup switches on, with the scope and audience each needs."""
    return [get_server(name) for name in SETUP_SERVER_NAMES]


def manifest_entry(server: WorkIqServer) -> dict[str, str]:
    """*server* as one entry of ToolingManifest.json's ``mcpServers``."""
    return {
        "mcpServerName": server.server_name,
        "mcpServerUniqueName": server.server_name,
        "url": server.default_endpoint,
        "scope": server.scope,
        "audience": server.audience,
        "publisher": PUBLISHER,
    }


def needed_scopes() -> dict[str, tuple[str, ...]]:
    """For each audience, the scopes the agent's token must carry for setup's servers."""
    needed: dict[str, list[str]] = {}
    for server in setup_servers():
        scopes = needed.setdefault(server.audience, [])
        if server.scope not in scopes:
            scopes.append(server.scope)
    return {audience: tuple(scopes) for audience, scopes in needed.items()}


def granted_scopes() -> dict[str, tuple[str, ...]]:
    """For each resource app, the delegated scopes the Blueprint is granted (and passes on to
    its agents): the servers' own, then any extra Microsoft's tool grants too."""
    needed = needed_scopes()
    granted: dict[str, tuple[str, ...]] = {}
    for app_id in [*_RESOURCE_ORDER, *needed]:
        if app_id in granted or app_id not in needed:
            continue
        extra = tuple(
            scope for scope in _EXTRA_GRANTED_SCOPES.get(app_id, ()) if scope not in needed[app_id]
        )
        granted[app_id] = needed[app_id] + extra
    return granted
