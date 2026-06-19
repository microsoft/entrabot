"""Enumerate every tool available to a Copilot SDK session, grouped into sections.

The session knows its full tool set once validated: ``rpc.tools.initialize_and_validate()``
then ``rpc.tools.get_current_metadata()`` returns all tool names — native tools (``powershell``,
``view``, ``edit``, ``grep``, …), the ``skill`` invoker, and MCP tools named ``<server>-<tool>``.
``rpc.mcp.list()`` gives the server names (to attribute MCP tools) and ``rpc.skills.list()`` the
individual skills. This is the catalog the per-caller permission matrix is built from.
"""

from __future__ import annotations

from typing import Any, List, Optional


async def enumerate_tools(session: Any) -> List[dict]:
    """Return ``[{name, section, kind}]`` for every tool + skill the session exposes.

    section: "Native" | "MCP · <server>" | "Skills". kind: "tool" | "skill".
    Best-effort: returns whatever it can resolve, never raises.
    """
    out: List[dict] = []

    # 1) the full tool set (must validate first or metadata comes back empty)
    tool_names: List[str] = []
    try:
        await session.rpc.tools.initialize_and_validate()
        md = await session.rpc.tools.get_current_metadata()
        for t in getattr(md, "tools", None) or []:
            name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            if name:
                tool_names.append(name)
    except Exception:
        pass

    # 2) MCP servers — to attribute "<server>-<tool>" names. Longest name first so
    #    "github-mcp-server" wins over a hypothetical "github".
    servers: List[str] = []
    try:
        mc = await session.rpc.mcp.list()
        servers = [getattr(s, "name", "") for s in (getattr(mc, "servers", None) or [])]
    except Exception:
        pass
    servers = sorted((s for s in servers if s), key=len, reverse=True)

    for name in tool_names:
        section = "Native"
        for srv in servers:
            if name.startswith(srv + "-"):
                section = f"MCP · {srv}"
                break
        out.append({"name": name, "section": section, "kind": "tool"})

    # 3) skills (invoked via the `skill` tool; listed individually so they can be shown)
    try:
        sk = await session.rpc.skills.list()
        for s in getattr(sk, "skills", None) or []:
            sname = getattr(s, "name", None) or (s.get("name") if isinstance(s, dict) else None)
            if sname:
                out.append({"name": sname, "section": "Skills", "kind": "skill"})
    except Exception:
        pass

    return out


def group_sections(catalog: List[dict]) -> "List[tuple]":
    """Group a catalog into ordered (section, [tool-dicts]) — Native, then MCP servers, then Skills."""
    order: dict = {}
    for item in catalog:
        order.setdefault(item["section"], []).append(item)

    def rank(section: str) -> tuple:
        if section == "Native":
            return (0, section)
        if section == "Skills":
            return (2, section)
        return (1, section)  # MCP servers in between, alphabetical

    return [(s, order[s]) for s in sorted(order, key=rank)]
