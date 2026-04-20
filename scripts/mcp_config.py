"""Dual-host MCP config writer used by ``scripts/setup.sh``.

EntraClaw supports two MCP clients: Claude Code (leader) and Copilot CLI
(slave). Both read a JSON config file listing the MCP servers to launch,
but from different locations:

  * Claude Code:  ``<project-root>/.mcp.json`` (project-local)
  * Copilot CLI:  ``$COPILOT_HOME/mcp-config.json``, defaulting to
                  ``~/.copilot/mcp-config.json`` (user-level, may also
                  be overridden by a project-local ``.mcp.json`` which
                  Copilot CLI honors too).

``setup.sh`` calls :func:`upsert_mcp_entry` against both paths so either
host picks the server up without extra steps. The entries are byte-
identical — the runtime leader/slave split happens dynamically via
``clientInfo.name`` at session initialize inside the MCP server itself,
NOT via separate configs.

See :func:`entraclaw.mcp_server._current_host` for the host detection,
and :func:`entraclaw.mcp_server._is_leader_host` for the gating.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

# Filename under ``$COPILOT_HOME`` (default ``~/.copilot``) where Copilot CLI
# stores its MCP server config. Documented at
# https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
COPILOT_CONFIG_REL_PATH = "mcp-config.json"


def copilot_config_path() -> Path:
    """Resolve the Copilot CLI MCP config path.

    Honors ``$COPILOT_HOME`` if set, else falls back to ``~/.copilot``.
    """
    home_override = os.environ.get("COPILOT_HOME")
    base = Path(home_override) if home_override else Path.home() / ".copilot"
    return base / COPILOT_CONFIG_REL_PATH


def build_entraclaw_entry(binary_path: str) -> dict:
    """Return the MCP server entry dict for the ``entraclaw`` server.

    Shape matches both Claude Code's and Copilot CLI's MCP config schema
    (``type``, ``command``, ``args``, ``description``).
    """
    return {
        "type": "stdio",
        "command": binary_path,
        "args": [],
        "description": (
            "EntraClaw Agent Identity — Teams tools + background DM/email poll "
            "(leader/slave auto-detected from MCP clientInfo)"
        ),
    }


def upsert_mcp_entry(
    target: Path, server_name: str, entry: dict
) -> None:
    """Insert or update an MCP server entry in a JSON config file.

    Idempotent: if ``server_name`` already exists with the same value,
    no-op; if it exists with a different value, overwrite. Other servers
    in the file are left alone. Top-level keys besides ``mcpServers``
    (e.g. Copilot's user settings) are preserved.

    If the target file is missing, creates it with only ``mcpServers``.
    If the target file is corrupt JSON, backs it up to
    ``<target>.bak.<unix-ts>`` and writes a fresh config.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {"mcpServers": {}}
    if target.is_file():
        try:
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("top-level JSON must be an object")
            if "mcpServers" not in data or not isinstance(
                data["mcpServers"], dict
            ):
                data["mcpServers"] = {}
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            backup = target.with_suffix(
                target.suffix + f".bak.{int(time.time())}"
            )
            # If rename fails, we still try to overwrite below.
            with contextlib.suppress(OSError):
                target.rename(backup)
            print(
                f"[mcp-config] warning: {target} was unreadable "
                f"({exc.__class__.__name__}: {exc}); backed up to {backup}",
                file=sys.stderr,
            )
            data = {"mcpServers": {}}

    data["mcpServers"][server_name] = entry

    target.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by ``setup.sh``.

    Usage:
        python scripts/mcp_config.py \
            --binary /path/to/entraclaw-mcp \
            --project-root /path/to/repo
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        required=True,
        help="Absolute path to the entraclaw-mcp binary.",
    )
    parser.add_argument(
        "--project-root",
        required=True,
        help="Project root (where .mcp.json lives).",
    )
    parser.add_argument(
        "--skip-copilot",
        action="store_true",
        help="Only write .mcp.json (skip Copilot CLI config).",
    )
    args = parser.parse_args(argv)

    entry = build_entraclaw_entry(args.binary)

    # 1. Project-local .mcp.json (Claude Code + Copilot CLI project override).
    claude_target = Path(args.project_root) / ".mcp.json"
    upsert_mcp_entry(claude_target, "entraclaw", entry)
    print(f"[mcp-config] wrote {claude_target}")

    # 2. Copilot CLI user-level config.
    if not args.skip_copilot:
        copilot_target = copilot_config_path()
        upsert_mcp_entry(copilot_target, "entraclaw", entry)
        print(f"[mcp-config] wrote {copilot_target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
