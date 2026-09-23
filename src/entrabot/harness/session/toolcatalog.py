"""Enumerate every tool available to a Copilot SDK session, grouped into sections.

The session knows its full tool set once validated: ``rpc.tools.initialize_and_validate()``
then ``rpc.tools.get_current_metadata()`` returns all tool names — native tools (``powershell``,
``view``, ``edit``, ``grep``, …), the ``skill`` invoker, and MCP tools named ``<server>-<tool>``.
``rpc.mcp.list()`` gives the server names (to attribute MCP tools) and ``rpc.skills.list()`` the
individual skills. This is the catalog the per-caller permission matrix is built from.
"""

from __future__ import annotations

from typing import Any

SECTION_NATIVE = "Native"
SECTION_MCP_PREFIX = "MCP · "
SECTION_SKILLS = "Skills"

KIND_TOOL = "tool"
KIND_SKILL = "skill"

KEY_NAME = "name"
KEY_SECTION = "section"
KEY_KIND = "kind"


def _extract_name(obj: Any) -> str | None:
    """Resolve a name from an SDK object: ``.name`` attr, then dict ``"name"`` key, then None."""
    name = getattr(obj, "name", None)
    if name:
        return name
    if isinstance(obj, dict):
        return obj.get("name")
    return None


def _resolve_section(tool_name: str, servers: list[str]) -> str:
    """Attribute a tool: ``MCP · <server>`` if it matches a server prefix, else ``Native``."""
    for server in servers:
        if tool_name.startswith(server + "-"):
            return f"{SECTION_MCP_PREFIX}{server}"
    return SECTION_NATIVE


async def enumerate_tools(session: Any) -> list[dict]:
    """Return ``[{name, section, kind}]`` for every tool + skill the session exposes.

    section: "Native" | "MCP · <server>" | "Skills". kind: "tool" | "skill".
    Best-effort: returns whatever it can resolve, never raises.
    """
    catalog: list[dict] = []

    # 1) the full tool set (must validate first or metadata comes back empty)
    tool_names: list[str] = []
    try:
        await session.rpc.tools.initialize_and_validate()
        tools_metadata = await session.rpc.tools.get_current_metadata()
        for tool in getattr(tools_metadata, "tools", None) or []:
            name = _extract_name(tool)
            if name:
                tool_names.append(name)
    except Exception:
        pass

    # 2) MCP servers — to attribute "<server>-<tool>" names. Longest name first so
    #    "github-mcp-server" wins over a hypothetical "github".
    servers: list[str] = []
    try:
        mcp_list_result = await session.rpc.mcp.list()
        mcp_servers = getattr(mcp_list_result, "servers", None) or []
        servers = [getattr(server, "name", "") for server in mcp_servers]
    except Exception:
        pass
    servers = sorted((server for server in servers if server), key=len, reverse=True)

    for name in tool_names:
        section = _resolve_section(name, servers)
        catalog.append({KEY_NAME: name, KEY_SECTION: section, KEY_KIND: KIND_TOOL})

    # 3) skills (invoked via the `skill` tool; listed individually so they can be shown)
    try:
        skills_result = await session.rpc.skills.list()
        for skill in getattr(skills_result, "skills", None) or []:
            skill_name = _extract_name(skill)
            if skill_name:
                catalog.append(
                    {KEY_NAME: skill_name, KEY_SECTION: SECTION_SKILLS, KEY_KIND: KIND_SKILL}
                )
    except Exception:
        pass

    return catalog


def group_sections(catalog: list[dict]) -> list[tuple]:
    """Group a catalog into ordered (section, [tool-dicts]): Native, MCP servers, then Skills."""
    grouped: dict = {}
    for item in catalog:
        grouped.setdefault(item[KEY_SECTION], []).append(item)

    def rank(section: str) -> tuple:
        if section == SECTION_NATIVE:
            return (0, section)
        if section == SECTION_SKILLS:
            return (2, section)
        return (1, section)  # MCP servers in between, alphabetical

    return [(section, grouped[section]) for section in sorted(grouped, key=rank)]
