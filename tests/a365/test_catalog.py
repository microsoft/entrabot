from __future__ import annotations

import pytest

from entrabot.a365.catalog import (
    COPILOT_SERVER_NAME,
    ODSP_SERVER_NAME,
    ONEDRIVE_SERVER_NAME,
    SHAREPOINT_SERVER_NAME,
    TEAMS_SERVER_NAME,
    WORD_SERVER_NAME,
    WorkIqServer,
    get_server,
    list_servers,
)
from entrabot.a365.errors import A365ServerNotConfiguredError


def test_catalog_contains_word_server() -> None:
    server = get_server(WORD_SERVER_NAME)

    assert server == WorkIqServer(
        server_name="mcp_WordServer",
        display_name="Work IQ Word",
        scope="McpServers.Word.All",
        audience="",
        default_endpoint="https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
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
