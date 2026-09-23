"""Regression tests for dependency compatibility boundaries."""

from pathlib import Path


def test_mcp_sdk_stays_on_v1_until_v2_migration() -> None:
    """The v1 FastMCP imports are incompatible with MCP Python SDK v2."""
    mcp_requirement = next(
        line.strip().strip('",')
        for line in Path("pyproject.toml").read_text().splitlines()
        if line.strip().startswith('"mcp[')
    )

    assert "<2" in mcp_requirement
