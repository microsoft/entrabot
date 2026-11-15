"""Scaffolding for a new ENTRABOT agent (port of Bootstrap/Scaffolder.cs).

Writes the config plus the primary context file (AGENT.md) and Copilot instructions,
seeded from the agent's description. Existing AGENT.md / instructions are left untouched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .. import config as cfgmod
from ..config import HarnessConfig

_AGENT_MD = """# {name}

{description}

## About this ENTRABOT

- **Name:** {name}
- **Surface:** Microsoft Teams (via the ENTRABOT harness)

## Context

This file is the primary context for the `{name}` agent. Describe the project,
conventions, goals, and the people/sponsors it works with here so the agent has the
background it needs.

## Guidelines

- Keep replies focused and useful; this agent talks to humans on Teams.
- Honor per-caller permission policy — some callers may not be allowed to trigger
  certain tools/commands.
- Ask for clarification when a request is ambiguous.
"""

_COPILOT_INSTRUCTIONS = """# Copilot instructions

You are **{name}**, an ENTRABOT agent that communicates with people over Microsoft Teams.

{description}

See [AGENT.md](../AGENT.md) for the full context.

## MCP servers

Configure MCP servers in a `.mcp.json` file in this directory (or `.vscode/mcp.json`)
using the standard MCP format — an `mcpServers` object mapping each server name to its
`command` + `args` + `env` (or a `url` for HTTP/SSE servers). Restart the harness after
editing (or use `/reload`).
"""


@dataclass
class ScaffoldResult:
    config_path: str
    agent_path: str
    created: list[str]


def _write_template(path: str, content: str) -> bool:
    """Write ``content`` to ``path`` only if it does not already exist. Returns True if written."""
    if os.path.exists(path):
        return False
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return True


def bootstrap(root: str, cfg: HarnessConfig) -> ScaffoldResult:
    created: list[str] = []
    os.makedirs(root, exist_ok=True)

    cfg.ensure_identity()
    cfgmod.save(root, cfg)
    created.append(cfgmod.config_path(root))

    github_dir = os.path.join(root, ".github")
    os.makedirs(github_dir, exist_ok=True)

    instr_path = os.path.join(github_dir, "copilot-instructions.md")
    if _write_template(
        instr_path, _COPILOT_INSTRUCTIONS.format(name=cfg.name, description=cfg.description)
    ):
        created.append(instr_path)

    agent_path = os.path.join(root, "AGENT.md")
    if _write_template(agent_path, _AGENT_MD.format(name=cfg.name, description=cfg.description)):
        created.append(agent_path)

    return ScaffoldResult(
        config_path=cfgmod.config_path(root), agent_path=agent_path, created=created
    )
