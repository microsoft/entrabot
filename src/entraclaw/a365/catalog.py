"""Known Microsoft Agent 365 Work IQ server catalog."""

from __future__ import annotations

from dataclasses import dataclass

from entraclaw.a365.errors import A365ServerNotConfiguredError

AGENT365_SERVER_BASE = "https://agent365.svc.cloud.microsoft/agents/servers"

WORD_SERVER_NAME = "mcp_WordServer"
MAIL_SERVER_NAME = "mcp_MailTools"
CALENDAR_SERVER_NAME = "mcp_CalendarTools"
ODSP_SERVER_NAME = "mcp_ODSPRemoteServer"
SHAREPOINT_SERVER_NAME = "mcp_SharePointRemoteServer"
ONEDRIVE_SERVER_NAME = "mcp_OneDriveRemoteServer"
USER_SERVER_NAME = "mcp_UserTools"
COPILOT_SERVER_NAME = "mcp_M365Copilot"
DATAVERSE_SERVER_NAME = "mcp_DataverseTools"
TEAMS_SERVER_NAME = "mcp_TeamsServer"


@dataclass(frozen=True)
class WorkIqServer:
    """Static metadata for one Work IQ MCP server."""

    server_name: str
    display_name: str
    scope: str
    audience: str
    default_endpoint: str
    entraclaw_enabled: bool = True


def _server(server_name: str, display_name: str, scope: str, enabled: bool = True) -> WorkIqServer:
    return WorkIqServer(
        server_name=server_name,
        display_name=display_name,
        scope=scope,
        audience="",
        default_endpoint=f"{AGENT365_SERVER_BASE}/{server_name}",
        entraclaw_enabled=enabled,
    )


_SERVERS: dict[str, WorkIqServer] = {
    WORD_SERVER_NAME: _server(WORD_SERVER_NAME, "Work IQ Word", "McpServers.Word.All"),
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
