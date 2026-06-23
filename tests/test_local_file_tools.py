"""Registration tests for the sandboxed local-file MCP tools.

``read_local_file`` and ``write_local_file`` are purpose-named, intent-matching
tools that wrap the MXC sandbox (clamp -> canonicalize -> Seatbelt). They are
gated behind the same ``ENTRABOT_ENABLE_RUN_CODE`` flag as ``run_code`` (they use
the same sandbox machinery) and must NOT be exposed when the sandbox is disabled.
"""

import asyncio
import importlib
import os
from unittest.mock import patch


def _registered_tool_names() -> list[str]:
    import entrabot.mcp_server as server

    return [t.name for t in asyncio.run(server.mcp.list_tools())]


def test_local_file_tools_not_registered_without_flag():
    import entrabot.mcp_server as server

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ENTRABOT_ENABLE_RUN_CODE", None)
        importlib.reload(server)
        names = _registered_tool_names()
    importlib.reload(server)  # restore real env
    assert "read_local_file" not in names
    assert "write_local_file" not in names


def test_local_file_tools_registered_with_flag():
    import entrabot.mcp_server as server

    with patch.dict(os.environ, {"ENTRABOT_ENABLE_RUN_CODE": "1"}, clear=False):
        importlib.reload(server)
        names = _registered_tool_names()
    importlib.reload(server)  # restore real env
    assert "read_local_file" in names
    assert "write_local_file" in names
    # The sandboxed write must coexist with run_code under the same gate.
    assert "run_code" in names
