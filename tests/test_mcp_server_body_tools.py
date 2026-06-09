"""Tests confirming the two new body-side tools are registered with FastMCP.

Issue #20: ``read_interactions`` and ``bootstrap_body_state`` must be
exposed through the MCP surface, not just available as Python imports.
These tests verify registration and the JSON-string contract the MCP
wrapper enforces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from entrabot import mcp_server
from entrabot.tools import interaction_log as il


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    monkeypatch.setenv("ENTRABOT_KEEP_MEMORY_LOCAL", "true")
    mcp_server._state.pop("config", None)
    yield tmp_path
    mcp_server._state.pop("config", None)


class TestToolRegistration:
    def test_read_interactions_is_registered(self) -> None:
        assert "read_interactions" in mcp_server.mcp._tool_manager._tools

    def test_bootstrap_body_state_is_registered(self) -> None:
        assert "bootstrap_body_state" in mcp_server.mcp._tool_manager._tools


class TestReadInteractionsThroughMCP:
    @pytest.mark.asyncio
    async def test_returns_json_string(self, tmp_data_dir: Path) -> None:
        il.log_interaction(channel="terminal", direction="inbound", sender="u", summary="hi")
        tool = mcp_server.mcp._tool_manager._tools["read_interactions"]
        out = await tool.fn()
        # MCP tools return JSON strings (matches read_email, list_promises convention)
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["summary"] == "hi"

    @pytest.mark.asyncio
    async def test_filters_passed_through(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:A@thread.v2",
            summary="to A",
        )
        il.log_interaction(
            channel="teams_group",
            direction="outbound",
            sender="agent",
            recipient="19:B@thread.v2",
            summary="to B",
        )
        tool = mcp_server.mcp._tool_manager._tools["read_interactions"]
        out = await tool.fn(chat_id="19:A@thread.v2")
        parsed = json.loads(out)
        assert len(parsed) == 1
        assert parsed[0]["recipient"] == "19:A@thread.v2"

    @pytest.mark.asyncio
    async def test_invalid_direction_returns_error_json(self, tmp_data_dir: Path) -> None:
        """Validation errors come back as JSON, not raw exceptions."""
        tool = mcp_server.mcp._tool_manager._tools["read_interactions"]
        out = await tool.fn(direction="sideways")
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
        assert "error" in parsed


class TestBootstrapBodyStateThroughMCP:
    @pytest.mark.asyncio
    async def test_returns_json_packet(self, tmp_data_dir: Path) -> None:
        tool = mcp_server.mcp._tool_manager._tools["bootstrap_body_state"]
        out = await tool.fn()
        parsed = json.loads(out)
        # All documented top-level keys present
        for key in (
            "today_counts",
            "top_chats_today",
            "open_promises",
            "cursor_freshness",
            "watched_chat_count",
            "generated_at",
        ):
            assert key in parsed

    @pytest.mark.asyncio
    async def test_reflects_logged_interactions(self, tmp_data_dir: Path) -> None:
        il.log_interaction(
            channel="teams_dm",
            direction="outbound",
            sender="agent",
            recipient="19:X@unq.gbl.spaces",
            summary="hi",
            action="send_teams_message",
        )
        tool = mcp_server.mcp._tool_manager._tools["bootstrap_body_state"]
        out = await tool.fn()
        parsed = json.loads(out)
        assert parsed["today_counts"]["total"] == 1
        assert parsed["today_counts"]["outbound"] == 1
        assert parsed["today_counts"]["by_action"]["send_teams_message"] == 1
