"""MCP discovery (port of Session/McpConfigLoader.cs).

Reads project-scoped ``.mcp.json`` / ``.vscode/mcp.json`` and returns a mapping of
server name -> SDK MCP config, ready to pass to ``create_session(mcp_servers=...)``.
Accepts both ``mcpServers`` and ``servers`` top-level keys; tolerates malformed files.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

_CANDIDATES = [".mcp.json", os.path.join(".vscode", "mcp.json")]

_BODY_TOOLS_CACHE: list[str] | None = None


def entrabot_body_tool_names() -> list[str]:
    """The entrabot MCP body's tool names, parsed from its source (the ``@mcp.tool()`` defs in
    ``entrabot/mcp_server.py``). The Copilot SDK auto-discovers the entrabot body from the user's
    ``~/.copilot/mcp-config.json`` (and any project ``.mcp.json``) regardless of what the harness
    passes as ``mcp_servers`` — so the harness excludes those tools by name to avoid duplicating
    its own Teams reply path. Source-derived so the list never drifts; [] if it can't be read."""
    global _BODY_TOOLS_CACHE
    if _BODY_TOOLS_CACHE is not None:
        return _BODY_TOOLS_CACHE
    try:
        import entrabot

        source_path = os.path.join(os.path.dirname(entrabot.__file__), "mcp_server.py")
        with open(source_path, encoding="utf-8") as handle:
            source = handle.read()
        _BODY_TOOLS_CACHE = re.findall(
            r"@mcp\.tool\([^)]*\)\s*\n\s*(?:async\s+)?def\s+(\w+)", source)
    except Exception:
        _BODY_TOOLS_CACHE = []
    return _BODY_TOOLS_CACHE


def _is_entrabot_body(name: str, server_config: dict[str, Any]) -> bool:
    """The entrabot MCP body (``entrabot-mcp``) duplicates what the harness already provides — the
    Teams reply tools and the background DM/email poll. Loading it inside the harness would double
    those and run two pollers, so it's filtered out. Matched by server name or command, so a
    renamed entry is still caught."""
    if name.strip().lower() == "entrabot":
        return True
    command = server_config.get("command")
    return isinstance(command, str) and os.path.basename(command).lower().startswith("entrabot-mcp")


def load(root: str, *, on_skip: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """Return ``{name: mcp_config_dict}`` or None if no config file is present. The self-referential
    entrabot MCP body is dropped (``on_skip`` is called with its name when so)."""
    raw = None
    for relative_path in _CANDIDATES:
        path = os.path.join(root, relative_path)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    raw = json.load(handle)
                break
            except (OSError, json.JSONDecodeError):
                return None
    if raw is None:
        return None

    servers = raw.get("mcpServers") or raw.get("servers")
    if not isinstance(servers, dict):
        return None

    result: dict[str, Any] = {}
    for name, server_config in servers.items():
        if not isinstance(server_config, dict):
            continue
        if _is_entrabot_body(name, server_config):
            if on_skip is not None:
                on_skip(name)
            continue
        parsed = _parse_one(server_config)
        if parsed is not None:
            result[name] = parsed
    return result or None


def _parse_one(server_config: dict[str, Any]) -> dict[str, Any] | None:
    tools = server_config.get("tools") or ["*"]
    if "url" in server_config:  # HTTP / SSE server
        server: dict[str, Any] = {
            "type": server_config.get("type", "http"),
            "url": server_config["url"],
            "tools": tools,
        }
        if server_config.get("headers"):
            server["headers"] = server_config["headers"]
        return server
    if "command" in server_config:  # stdio server
        server = {
            "type": "stdio",
            "command": server_config["command"],
            "tools": tools,
        }
        if server_config.get("args"):
            server["args"] = server_config["args"]
        if server_config.get("env"):
            server["env"] = server_config["env"]
        working_directory = server_config.get("cwd") or server_config.get("working_directory")
        if working_directory:
            server["working_directory"] = working_directory
        return server
    return None
