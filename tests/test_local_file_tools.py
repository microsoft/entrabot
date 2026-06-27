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


# ── error discrimination: sandbox-helper spawn failure vs blocked path ───────
def _result(exit_code, stderr):
    from entrabot.sandbox.base import SandboxResult

    return SandboxResult(
        exit_code=exit_code, stdout="", stderr=stderr, duration_ms=1, timed_out=False
    )


def test_spawn_failure_signature_is_detected():
    from entrabot.mcp_server import _is_sandbox_spawn_failure

    assert _is_sandbox_spawn_failure("CreateProcessW failed: ERROR_FILE_NOT_FOUND")
    assert _is_sandbox_spawn_failure("backend_error: 0x80070002")
    # A genuine policy denial is NOT a spawn failure.
    assert not _is_sandbox_spawn_failure("Access is denied.")
    assert not _is_sandbox_spawn_failure("Operation not permitted")
    assert not _is_sandbox_spawn_failure("")


def test_read_handler_distinguishes_spawn_failure_from_blocked_path():
    from entrabot.mcp_server import _local_file_failure_response

    # The documented Windows spawn-failure signature -> distinct internal error.
    spawn = _local_file_failure_response(
        _result(1, "CreateProcessW failed: ERROR_FILE_NOT_FOUND (0x80070002)"),
        operation="read",
        path="C:\\Users\\me\\notes.txt",
    )
    assert spawn["error"] == "Sandbox helper could not run the command"
    assert "internal sandbox configuration" in spawn["help"]
    assert "outside" not in spawn["help"]  # NOT the blocked-path message

    # A generic nonzero inner exit -> the existing blocked/outside-ceiling message.
    blocked = _local_file_failure_response(
        _result(1, "Operation not permitted"),
        operation="read",
        path="/secret/x.txt",
    )
    assert blocked["error"] == "Read blocked or failed"
    assert "outside the sandbox's allowed read paths" in blocked["help"]


def test_write_handler_distinguishes_spawn_failure_from_blocked_path():
    from entrabot.mcp_server import _local_file_failure_response

    spawn = _local_file_failure_response(
        _result(1, "backend_error: CreateProcessW failed"),
        operation="write",
        path="C:\\out\\note.txt",
    )
    assert spawn["error"] == "Sandbox helper could not run the command"
    assert "NOT a blocked path" in spawn["help"]

    blocked = _local_file_failure_response(
        _result(1, "Access is denied."),
        operation="write",
        path="C:\\Windows\\x.txt",
    )
    assert blocked["error"] == "Write blocked or failed"
    assert "outside the sandbox's allowed write paths" in blocked["help"]
