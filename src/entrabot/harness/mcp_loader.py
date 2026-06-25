"""MCP discovery (port of Session/McpConfigLoader.cs).

Reads project-scoped ``.mcp.json`` / ``.vscode/mcp.json`` and returns a mapping of
server name -> SDK MCP config, ready to pass to ``create_session(mcp_servers=...)``.
Accepts both ``mcpServers`` and ``servers`` top-level keys; tolerates malformed files.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Dict, Optional

_CANDIDATES = [".mcp.json", os.path.join(".vscode", "mcp.json")]


def _is_entrabot_body(name: str, conf: dict[str, Any]) -> bool:
    """The entrabot MCP body (``entrabot-mcp``) duplicates what the harness already provides — the
    Teams reply tools and the background DM/email poll. Loading it inside the harness would double
    those and run two pollers, so it's filtered out. Matched by server name or command, so a
    renamed entry is still caught."""
    if name.strip().lower() == "entrabot":
        return True
    command = conf.get("command")
    return isinstance(command, str) and os.path.basename(command).lower().startswith("entrabot-mcp")


def load(root: str, *, on_skip: Callable[[str], None] | None = None) -> Optional[Dict[str, Any]]:
    """Return ``{name: mcp_config_dict}`` or None if no config file is present. The self-referential
    entrabot MCP body is dropped (``on_skip`` is called with its name when so)."""
    raw = None
    for rel in _CANDIDATES:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                break
            except (OSError, json.JSONDecodeError):
                return None
    if raw is None:
        return None

    servers = raw.get("mcpServers") or raw.get("servers")
    if not isinstance(servers, dict):
        return None

    out: Dict[str, Any] = {}
    for name, conf in servers.items():
        if not isinstance(conf, dict):
            continue
        if _is_entrabot_body(name, conf):
            if on_skip is not None:
                on_skip(name)
            continue
        parsed = _parse_one(conf)
        if parsed is not None:
            out[name] = parsed
    return out or None


def _parse_one(conf: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tools = conf.get("tools") or ["*"]
    if "url" in conf:  # HTTP / SSE server
        server: Dict[str, Any] = {
            "type": conf.get("type", "http"),
            "url": conf["url"],
            "tools": tools,
        }
        if conf.get("headers"):
            server["headers"] = conf["headers"]
        return server
    if "command" in conf:  # stdio server
        server = {
            "type": "stdio",
            "command": conf["command"],
            "tools": tools,
        }
        if conf.get("args"):
            server["args"] = conf["args"]
        if conf.get("env"):
            server["env"] = conf["env"]
        if conf.get("cwd") or conf.get("working_directory"):
            server["working_directory"] = conf.get("cwd") or conf.get("working_directory")
        return server
    return None
