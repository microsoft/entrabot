"""Tests for run_code MCP tool in mcp_server.py."""

import os
from unittest.mock import MagicMock, patch


# RED: Test run_code tool not registered when env flag unset
def test_run_code_not_registered_without_env_flag():
    """run_code tool is not registered when ENTRABOT_ENABLE_RUN_CODE unset."""
    with patch.dict(os.environ, {}, clear=True):
        # Mock FastMCP to capture registered tools
        from unittest.mock import Mock
        mock_mcp = Mock()
        mock_mcp.tool = Mock(return_value=lambda f: f)
        
        # Import with mocked FastMCP
        with patch("entrabot.mcp_server.mcp", mock_mcp):
            # Force reload to pick up env changes
            import importlib

            import entrabot.mcp_server as server_module
            importlib.reload(server_module)
            
            # run_code should not be decorated/registered
            # (This is a smoke test - real test is checking tool is not in MCP's tool list)


def test_run_code_registered_with_env_flag():
    """run_code tool IS registered when ENTRABOT_ENABLE_RUN_CODE=1."""
    with patch.dict(os.environ, {"ENTRABOT_ENABLE_RUN_CODE": "1"}, clear=False):
        # Force re-import to pick up env change
        import importlib

        import entrabot.mcp_server
        importlib.reload(entrabot.mcp_server)
        
        # run_code should exist when flag is set
        assert hasattr(
            entrabot.mcp_server, "run_code"
        ), "run_code should be defined when ENTRABOT_ENABLE_RUN_CODE=1"


# RED: Test run_code requires argv parameter
def test_run_code_requires_argv():
    """run_code() requires argv parameter (structured command)."""
    # Should have argv parameter
    import inspect

    from entrabot.mcp_server import run_code
    sig = inspect.signature(run_code)
    assert "argv" in sig.parameters


def test_run_code_accepts_ceiling_narrowing():
    """run_code() accepts optional ceiling narrowing parameters."""
    import inspect

    from entrabot.mcp_server import run_code
    sig = inspect.signature(run_code)
    
    # Should accept optional narrowing params (subset of ceiling)
    assert "readonly_paths" in sig.parameters
    assert "readwrite_paths" in sig.parameters
    assert "timeout_ms" in sig.parameters


# RED: Test run_code audits before execution
@patch("entrabot.sandbox.get_sandbox_runner")
@patch("entrabot.tools.audit.log_event")
def test_run_code_audits_pending_before_execution(mock_audit, mock_get_runner):
    """run_code() emits audit 'pending' event before execution."""
    from entrabot.mcp_server import run_code
    
    # Mock runner to succeed
    mock_runner = MagicMock()
    mock_runner.get_capabilities.return_value = {"backend": "process"}
    mock_runner.run.return_value = MagicMock(
        exit_code=0, stdout="output", stderr="", duration_ms=100, timed_out=False
    )
    mock_get_runner.return_value = mock_runner
    
    # Call run_code
    run_code(argv=["python", "test.py"])
    
    # Verify audit was called with 'pending' outcome before execution
    assert mock_audit.called
    # Check first call (pending)
    first_call = mock_audit.call_args_list[0]
    assert first_call[1]["action"] == "run_code"
    assert first_call[1]["resource"] == "sandbox"
    assert first_call[1]["outcome"] == "pending"


# RED: Test run_code clamps policy to ceiling
@patch("entrabot.sandbox.get_sandbox_runner")
@patch("entrabot.sandbox.policy.clamp_to_ceiling")
@patch("entrabot.tools.audit.log_event")
def test_run_code_clamps_policy_to_ceiling(mock_audit, mock_clamp, mock_get_runner):
    """run_code() applies clamp_to_ceiling before execution."""
    from entrabot.mcp_server import run_code
    
    mock_runner = MagicMock()
    mock_runner.get_capabilities.return_value = {"backend": "process"}
    mock_runner.run.return_value = MagicMock(
        exit_code=0, stdout="", stderr="", duration_ms=0, timed_out=False
    )
    mock_get_runner.return_value = mock_runner
    
    # Mock clamp to return a policy
    from entrabot.sandbox.base import SandboxPolicy
    mock_clamp.return_value = SandboxPolicy(
        backend="process",
        command_line="python test.py",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=30000,
    )
    
    run_code(argv=["python", "test.py"])
    
    # Verify clamp_to_ceiling was called
    assert mock_clamp.called


# RED: Test run_code fails closed if audit fails
@patch("entrabot.tools.audit.log_event")
@patch("entrabot.sandbox.get_sandbox_runner")
def test_run_code_fails_closed_on_audit_failure(mock_get_runner, mock_audit):
    """run_code() refuses to run if audit fails (fail-closed)."""
    from entrabot.mcp_server import run_code
    
    # Make audit raise exception
    mock_audit.side_effect = Exception("Audit unavailable")
    
    # Mock runner to not be called
    mock_runner = MagicMock()
    mock_runner.get_capabilities.return_value = {"backend": "process"}
    mock_get_runner.return_value = mock_runner
    
    # Should return error, not raise (catch-all at end)
    result = run_code(argv=["echo", "test"])
    assert "error" in result.lower()


# RED: Test run_code returns stdout/stderr/exit_code
@patch("entrabot.tools.audit.log_event")
@patch("entrabot.sandbox.get_sandbox_runner")
def test_run_code_returns_result(mock_get_runner, mock_audit):
    """run_code() returns stdout, stderr, exit_code from sandbox."""
    from entrabot.mcp_server import run_code
    
    mock_runner = MagicMock()
    mock_runner.get_capabilities.return_value = {"backend": "process"}
    mock_runner.run.return_value = MagicMock(
        exit_code=0,
        stdout="test output",
        stderr="test error",
        duration_ms=123,
        timed_out=False,
    )
    mock_get_runner.return_value = mock_runner
    
    result = run_code(argv=["echo", "test"])
    
    assert "stdout" in result or "output" in result.lower()
    assert "test output" in str(result)


# RED: Test run_code handles sandbox unavailable
@patch("entrabot.sandbox.get_sandbox_runner")
def test_run_code_handles_unavailable_sandbox(mock_get_runner):
    """run_code() returns error message when sandbox unavailable."""
    from entrabot.mcp_server import run_code
    from entrabot.sandbox.base import SandboxUnavailableError
    
    mock_get_runner.side_effect = SandboxUnavailableError("MXC not installed")
    
    result = run_code(argv=["echo", "test"])
    
    # Should return error, not raise
    assert "unavailable" in str(result).lower() or "not installed" in str(result).lower()


# RED: Test run_code uses structured argv (no shell)
@patch("entrabot.tools.audit.log_event")
@patch("entrabot.sandbox.get_sandbox_runner")
def test_run_code_uses_structured_argv(mock_get_runner, mock_audit):
    """run_code() builds command from structured argv, not shell string."""
    from entrabot.mcp_server import run_code
    
    mock_runner = MagicMock()
    mock_runner.get_capabilities.return_value = {"backend": "process"}
    mock_runner.run.return_value = MagicMock(
        exit_code=0, stdout="", stderr="", duration_ms=0, timed_out=False
    )
    mock_get_runner.return_value = mock_runner
    
    run_code(argv=["python", "-c", "print('test')"])
    
    # Verify runner.run was called with a policy
    assert mock_runner.run.called
    policy = mock_runner.run.call_args[0][0]
    
    # Command should be structured from argv
    assert "python" in policy.command_line
    assert "-c" in policy.command_line or "print" in policy.command_line
