from __future__ import annotations

import pytest

from entrabot.a365.catalog import (
    COPILOT_SERVER_NAME,
    EXCEL_SERVER_NAME,
    ODSP_SERVER_NAME,
    ONEDRIVE_SERVER_NAME,
    SHAREPOINT_SERVER_NAME,
    TEAMS_SERVER_NAME,
    WORD_SERVER_NAME,
    WorkIqServer,
    get_server,
    granted_scopes,
    list_servers,
    manifest_entry,
    needed_scopes,
    setup_servers,
)
from entrabot.a365.errors import A365ServerNotConfiguredError


def test_catalog_contains_word_server() -> None:
    server = get_server(WORD_SERVER_NAME)

    # As Microsoft's own tool writes it into ToolingManifest.json for a working agent: Word has
    # its own resource app, and its scope is Tools.ListInvoke.All.
    assert server == WorkIqServer(
        server_name="mcp_WordServer",
        display_name="Work IQ Word",
        scope="Tools.ListInvoke.All",
        audience="c2d0c2b6-8013-4346-9f8b-b81d3b754a29",
        default_endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
        entrabot_enabled=True,
    )


def test_catalog_contains_excel_server() -> None:
    server = get_server(EXCEL_SERVER_NAME)

    assert server == WorkIqServer(
        server_name="mcp_ExcelServer",
        display_name="Work IQ Excel",
        scope="McpServers.Excel.All",
        audience="ea9ffc3e-8a23-4a7d-836d-234d7c7565c1",
        default_endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_ExcelServer",
        entrabot_enabled=True,
    )


def test_catalog_keeps_teams_disabled_for_entrabot() -> None:
    server = get_server(TEAMS_SERVER_NAME)

    assert server.server_name == "mcp_TeamsServer"
    assert server.entrabot_enabled is False


def test_catalog_contains_live_loop_candidate_servers() -> None:
    assert get_server(ODSP_SERVER_NAME).server_name == "mcp_ODSPRemoteServer"
    assert get_server(ONEDRIVE_SERVER_NAME).server_name == "mcp_OneDriveRemoteServer"
    assert get_server(SHAREPOINT_SERVER_NAME).server_name == "mcp_SharePointRemoteServer"
    assert get_server(COPILOT_SERVER_NAME).server_name == "mcp_M365Copilot"


def test_list_servers_returns_copy() -> None:
    servers = list_servers()
    servers.clear()

    assert get_server(WORD_SERVER_NAME).server_name == "mcp_WordServer"


def test_unknown_server_raises() -> None:
    with pytest.raises(A365ServerNotConfiguredError) as exc_info:
        get_server("mcp_Missing")

    assert "mcp_Missing" in str(exc_info.value)


# The ToolingManifest.json of a working agent, as Microsoft's Agent 365 tool wrote it. Setup now
# writes the same three entries itself.
WORKING_MANIFEST = [
    {
        "mcpServerName": "mcp_WordServer",
        "mcpServerUniqueName": "mcp_WordServer",
        "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
        "scope": "Tools.ListInvoke.All",
        "audience": "c2d0c2b6-8013-4346-9f8b-b81d3b754a29",
        "publisher": "Microsoft",
    },
    {
        "mcpServerName": "mcp_ODSPRemoteServer",
        "mcpServerUniqueName": "mcp_ODSPRemoteServer",
        "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_ODSPRemoteServer",
        "scope": "McpServers.OneDriveSharepoint.All",
        "audience": "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1",
        "publisher": "Microsoft",
    },
    {
        "mcpServerName": "mcp_ExcelServer",
        "mcpServerUniqueName": "mcp_ExcelServer",
        "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_ExcelServer",
        "scope": "McpServers.Excel.All",
        "audience": "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1",
        "publisher": "Microsoft",
    },
]


def test_setup_writes_the_manifest_microsofts_tool_writes() -> None:
    assert [manifest_entry(server) for server in setup_servers()] == WORKING_MANIFEST


def test_what_the_agent_needs_in_each_token() -> None:
    assert needed_scopes() == {
        "c2d0c2b6-8013-4346-9f8b-b81d3b754a29": ("Tools.ListInvoke.All",),
        "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1": (
            "McpServers.OneDriveSharepoint.All",
            "McpServers.Excel.All",
        ),
    }


def test_what_the_blueprint_is_granted_includes_the_tools_metadata_scope() -> None:
    # Microsoft's tool grants the metadata scope too; the agent's working token carries it.
    assert granted_scopes() == {
        "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1": (
            "McpServers.OneDriveSharepoint.All",
            "McpServers.Excel.All",
            "McpServersMetadata.Read.All",
        ),
        "c2d0c2b6-8013-4346-9f8b-b81d3b754a29": ("Tools.ListInvoke.All",),
    }
