"""Tests for the dual-host MCP config writer used by scripts/setup.sh.

The setup script writes the EntraClaw MCP server entry into two places:

  * project-local ``.mcp.json`` — Claude Code picks this up
  * ``~/.copilot/mcp-config.json`` (overridable via ``COPILOT_HOME``) —
    Copilot CLI picks this up

Both entries are byte-identical: same command, same args, same
description. The runtime leader/slave split is done via clientInfo at
session initialize — not via separate configs.

The writer must be idempotent: running it twice produces the same file
content, and it never clobbers unrelated server entries that already
live in either config. If the target file is missing it's created;
if present and contains valid JSON, the entraclaw entry is merged in.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "mcp_config.py"


@pytest.fixture
def mcp_config():
    """Load scripts/mcp_config.py as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("mcp_config", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_config"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("mcp_config", None)


class TestBuildEntraclawEntry:
    def test_entry_uses_given_binary(self, mcp_config) -> None:
        entry = mcp_config.build_entraclaw_entry("/foo/bar/entraclaw-mcp")
        assert entry["command"] == "/foo/bar/entraclaw-mcp"
        assert entry["type"] == "stdio"
        assert entry["args"] == []
        assert "description" in entry


class TestUpsertMcpEntry:
    def test_creates_file_when_missing(self, mcp_config, tmp_path: Path) -> None:
        target = tmp_path / "mcp-config.json"
        mcp_config.upsert_mcp_entry(target, "entraclaw", {"type": "stdio", "command": "/x"})

        data = json.loads(target.read_text())
        assert data == {
            "mcpServers": {
                "entraclaw": {"type": "stdio", "command": "/x"},
            }
        }

    def test_is_idempotent(self, mcp_config, tmp_path: Path) -> None:
        """Running the upsert twice leaves one entry, not two."""
        target = tmp_path / "mcp-config.json"
        entry = {"type": "stdio", "command": "/x"}

        mcp_config.upsert_mcp_entry(target, "entraclaw", entry)
        first = target.read_text()

        mcp_config.upsert_mcp_entry(target, "entraclaw", entry)
        second = target.read_text()

        assert first == second
        data = json.loads(second)
        assert list(data["mcpServers"].keys()) == ["entraclaw"]

    def test_merges_alongside_existing_entries(self, mcp_config, tmp_path: Path) -> None:
        """Unrelated servers (e.g. persona-sati) are preserved."""
        target = tmp_path / "mcp-config.json"
        target.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "persona-sati": {
                            "type": "sse",
                            "url": "http://localhost:8100/sse",
                        },
                    }
                }
            )
        )

        mcp_config.upsert_mcp_entry(target, "entraclaw", {"type": "stdio", "command": "/x"})

        data = json.loads(target.read_text())
        assert set(data["mcpServers"]) == {"persona-sati", "entraclaw"}
        assert data["mcpServers"]["persona-sati"]["url"] == "http://localhost:8100/sse"

    def test_updates_existing_entraclaw_entry(self, mcp_config, tmp_path: Path) -> None:
        """If the entraclaw entry already exists with a stale binary path,
        upserting a new entry overwrites it rather than duplicating."""
        target = tmp_path / "mcp-config.json"
        target.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "entraclaw": {"type": "stdio", "command": "/old/bin"},
                    }
                }
            )
        )

        mcp_config.upsert_mcp_entry(target, "entraclaw", {"type": "stdio", "command": "/new/bin"})

        data = json.loads(target.read_text())
        assert data["mcpServers"]["entraclaw"]["command"] == "/new/bin"

    def test_preserves_top_level_keys(self, mcp_config, tmp_path: Path) -> None:
        """If Copilot's config has top-level keys other than ``mcpServers``
        (e.g. user settings), the upsert leaves them untouched."""
        target = tmp_path / "mcp-config.json"
        target.write_text(
            json.dumps(
                {
                    "mcpServers": {},
                    "telemetry": {"enabled": False},
                }
            )
        )

        mcp_config.upsert_mcp_entry(target, "entraclaw", {"type": "stdio", "command": "/x"})

        data = json.loads(target.read_text())
        assert data["telemetry"] == {"enabled": False}
        assert "entraclaw" in data["mcpServers"]

    def test_handles_missing_parent_directory(self, mcp_config, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "dir" / "mcp-config.json"
        assert not target.parent.exists()

        mcp_config.upsert_mcp_entry(target, "entraclaw", {"type": "stdio", "command": "/x"})

        assert target.is_file()

    def test_corrupt_file_is_replaced(self, mcp_config, tmp_path: Path) -> None:
        """A file that isn't valid JSON shouldn't crash setup. The writer
        backs up the corrupt file and writes a fresh config."""
        target = tmp_path / "mcp-config.json"
        target.write_text("{not valid json")

        mcp_config.upsert_mcp_entry(target, "entraclaw", {"type": "stdio", "command": "/x"})

        data = json.loads(target.read_text())
        assert "entraclaw" in data["mcpServers"]
        backups = list(tmp_path.glob("mcp-config.json.bak.*"))
        assert len(backups) == 1


class TestCopilotConfigRelPath:
    def test_is_mcp_config_json(self, mcp_config) -> None:
        """Confirmed from Copilot CLI docs:
        ``~/.copilot/mcp-config.json`` (COPILOT_HOME overrides ~/.copilot).
        """
        assert mcp_config.COPILOT_CONFIG_REL_PATH == "mcp-config.json"
