"""Tests for unsafe_write_local_file demonstration tool in mcp_server.py.

This tool exists to demonstrate WHY sandboxing is necessary by providing
an UNPROTECTED file-write capability that contrasts with sandboxed run_code.
It is gated OFF by default — registering it would give the agent an
unsandboxed write path that bypasses run_code containment.
"""

import asyncio
import importlib
import os
import tempfile
from unittest.mock import patch


def _registered_tool_names() -> list[str]:
    import entrabot.mcp_server as server

    return [t.name for t in asyncio.run(server.mcp.list_tools())]


# RED: the unsafe tool must NOT be exposed to the agent by default.
def test_unsafe_write_local_file_not_registered_as_tool_by_default():
    """unsafe_write_local_file is NOT an MCP tool unless explicitly enabled.

    It writes anywhere with no containment, so exposing it by default would
    defeat the whole point of the sandbox (the agent could bypass run_code).
    """
    import entrabot.mcp_server as server

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ENTRABOT_ENABLE_UNSAFE_WRITE", None)
        importlib.reload(server)
        names = _registered_tool_names()
    importlib.reload(server)  # restore real env
    assert "unsafe_write_local_file" not in names


def test_unsafe_write_local_file_registered_when_explicitly_enabled():
    """unsafe_write_local_file IS exposed when ENTRABOT_ENABLE_UNSAFE_WRITE=1."""
    import entrabot.mcp_server as server

    with patch.dict(os.environ, {"ENTRABOT_ENABLE_UNSAFE_WRITE": "1"}, clear=False):
        importlib.reload(server)
        names = _registered_tool_names()
    importlib.reload(server)  # restore real env
    assert "unsafe_write_local_file" in names


# The function itself remains importable/callable for unit tests regardless of
# whether it's registered as an MCP tool.
def test_unsafe_write_local_file_exists():
    """unsafe_write_local_file function is defined and callable."""
    from entrabot.mcp_server import unsafe_write_local_file
    
    assert unsafe_write_local_file is not None
    assert callable(unsafe_write_local_file)


# RED: Test basic file write
def test_unsafe_write_local_file_creates_file():
    """unsafe_write_local_file() should create file with content."""
    import json

    from entrabot.mcp_server import unsafe_write_local_file
    
    with tempfile.TemporaryDirectory() as tmpdir:
        test_path = os.path.join(tmpdir, "test.txt")
        result_json = unsafe_write_local_file(path=test_path, content="Hello, world!")
        result = json.loads(result_json)
        
        assert result["success"] is True
        assert result["path"] == test_path
        assert os.path.exists(test_path)
        
        with open(test_path) as f:
            assert f.read() == "Hello, world!"


# RED: Test dangerous path (no validation - intentional!)
def test_unsafe_write_local_file_accepts_any_path():
    """unsafe_write_local_file() should accept ANY path (demonstrates danger)."""
    import json

    from entrabot.mcp_server import unsafe_write_local_file
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Try to write to a "sensitive" location (mocked as tmpdir)
        sensitive_path = os.path.join(tmpdir, "sensitive", "system.conf")
        os.makedirs(os.path.dirname(sensitive_path), exist_ok=True)
        
        result_json = unsafe_write_local_file(path=sensitive_path, content="hacked")
        result = json.loads(result_json)
        
        # Should succeed (this is the danger we're demonstrating!)
        assert result["success"] is True
        assert os.path.exists(sensitive_path)


# RED: Test error handling
def test_unsafe_write_local_file_handles_permission_error():
    """unsafe_write_local_file() should return error dict on permission failure."""
    import json

    from entrabot.mcp_server import unsafe_write_local_file
    
    # Try to write to a path that will fail (permission denied)
    bad_path = "/root/protected.txt"  # Assuming we don't have root
    
    result_json = unsafe_write_local_file(path=bad_path, content="fail")
    result = json.loads(result_json)
    
    # Should fail gracefully
    assert "error" in result or result.get("success") is False


# RED: Test audit logging
@patch("entrabot.tools.audit.log_event")
def test_unsafe_write_local_file_audits_actions(mock_audit):
    """unsafe_write_local_file() should emit audit events."""
    from entrabot.mcp_server import unsafe_write_local_file
    
    with tempfile.TemporaryDirectory() as tmpdir:
        test_path = os.path.join(tmpdir, "audit_test.txt")
        unsafe_write_local_file(path=test_path, content="test")
        
        # Verify audit was called
        assert mock_audit.called
        # Check it logged the dangerous file write
        calls = mock_audit.call_args_list
        assert any("unsafe_write_local_file" in str(call) for call in calls)


# RED: Test warning message in docstring
def test_unsafe_write_local_file_has_warning_docstring():
    """unsafe_write_local_file() docstring should include WARNING about danger."""
    from entrabot.mcp_server import unsafe_write_local_file
    
    docstring = unsafe_write_local_file.__doc__
    assert docstring is not None
    assert "WARNING" in docstring or "DANGER" in docstring or "UNPROTECTED" in docstring
    assert "sandboxing" in docstring.lower() or "sandbox" in docstring.lower()


# RED: Test comparison with sandboxed alternative
def test_demo_scenario_unsafe_vs_safe():
    """Demonstrate unsafe unsafe_write_local_file vs safe run_code."""
    import json

    from entrabot.mcp_server import unsafe_write_local_file
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # UNSAFE: Direct file write (no protection)
        unsafe_path = os.path.join(tmpdir, "unsafe.txt")
        unsafe_result = json.loads(unsafe_write_local_file(path=unsafe_path, content="no sandbox"))
        assert unsafe_result["success"] is True
        assert os.path.exists(unsafe_path)
        
        # SAFE: Would use run_code with sandboxed filesystem
        # (We can't test this without full integration, but document the pattern)
        # run_code(argv=["python", "-c", f"open('{safe_path}', 'w').write('sandboxed')"])
        # → would be clamped to operator ceiling (/tmp only)


# RED: Test that tool is always registered (not gated by flag)
def test_unsafe_write_local_file_always_available():
    """The unsafe_write_local_file *function* is always defined (importable for tests),
    independent of ENTRABOT_ENABLE_RUN_CODE. Whether it's exposed to the agent as
    an MCP tool is governed separately by ENTRABOT_ENABLE_UNSAFE_WRITE (see the
    registration tests above) — by default it is NOT registered.
    """
    with patch.dict(os.environ, {"ENTRABOT_ENABLE_RUN_CODE": "0"}):
        import importlib

        import entrabot.mcp_server
        importlib.reload(entrabot.mcp_server)
        
        assert hasattr(entrabot.mcp_server, "unsafe_write_local_file")
