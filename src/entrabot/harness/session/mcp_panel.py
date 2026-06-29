"""The ``/mcp`` panel: list the MCP servers this session has — installed (this dir's
.mcp.json) and what the Copilot SDK discovered + connected live."""

from __future__ import annotations

import copilot

from ..ui import UiStyle
from . import mcp_loader


class _McpPanelMixin:
    async def _live_mcp_servers(self):
        """The MCP servers the Copilot SDK actually discovered + connected this session (user
        ~/.copilot config + project .mcp.json + builtins), as (name, source, status). [] if the
        session can't report them. This is what the SDK has configured — the discovery the harness
        keeps on; it's a superset of this dir's .mcp.json (e.g. the user's github MCP)."""
        def _enum_value(value):  # enum -> "builtin"/"connected"; str passthrough
            return str(getattr(value, "value", value) or "?")

        try:
            result = await self._session.rpc.mcp.list()
            servers = []
            for server in getattr(result, "servers", None) or []:
                servers.append((
                    getattr(server, "name", "?"),
                    _enum_value(getattr(server, "source", None)),
                    _enum_value(getattr(server, "status", None)),
                ))
            return servers
        except Exception:
            return []

    def _body_excluded_tools(self):
        """A ToolSet excluding the entrabot MCP body's tools (named ``entrabot-<tool>`` once the
        SDK loads the discovered server), so the harness's own ``entrabot_send``/``read``/
        ``list_chats`` natives (underscore, not MCP-classified) are the only Teams reply path."""
        excluded = copilot.ToolSet()
        for name in mcp_loader.entrabot_body_tool_names():
            excluded.add_mcp(f"entrabot-{name}")
        return excluded if len(excluded) else None

    @staticmethod
    def _mcp_rows(mcp: dict, live: list) -> list[str]:
        """Display lines for /mcp: Installed (this dir's .mcp.json) · Discovered (live, SDK)."""
        rows: list[str] = ["── Installed (this dir's .mcp.json) ──"]
        if mcp:
            for name, server_config in mcp.items():
                server_type = (
                    server_config.get("type", "stdio") if isinstance(server_config, dict) else "?"
                )
                rows.append(f"   {name:<22}[{server_type}]")
        else:
            rows.append("   (none)")

        # What the Copilot SDK actually discovered + connected (user ~/.copilot config, project
        # .mcp.json, builtins). entrabot is discovered but its tools are excluded.
        if live:
            rows.append("── Discovered by the CLI (live) ──")
            for name, source, status in live:
                blocked = "  · tools blocked by harness" if name == "entrabot" else ""
                rows.append(f"   {name:<22}{source}/{status}{blocked}")
        return rows

    async def _handle_mcp(self) -> None:
        """Show the MCP servers this session has: installed (this dir's .mcp.json) and what the
        Copilot SDK discovered + connected live."""
        mcp = mcp_loader.load(self._root) or {}
        live = await self._live_mcp_servers()
        for row in self._mcp_rows(mcp, live):
            self._ui.append_line(row, UiStyle.INFO)
