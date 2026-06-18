"""Tests for sandbox/mac.py — macOS Seatbelt runner."""

import json
from unittest.mock import patch

import pytest


# RED: Test SeatbeltRunner implements protocol
def test_seatbelt_runner_implements_protocol():
    """SeatbeltRunner implements SandboxRunner protocol."""
    from entrabot.sandbox.mac import SeatbeltRunner
    
    runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
    
    # Should have required methods
    assert callable(runner.run)
    assert callable(runner.get_capabilities)
    assert callable(runner.identity_binding)


# RED: Test get_capabilities returns backend info
def test_seatbelt_runner_capabilities():
    """get_capabilities() returns seatbelt backend capabilities."""
    from entrabot.sandbox.mac import SeatbeltRunner
    
    runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
    caps = runner.get_capabilities()
    
    assert caps["backend"] == "seatbelt"
    assert caps["network_host_filtering"] is False  # macOS can't filter by host
    assert caps["deny_paths_supported"] is False  # Not using deniedPaths


# RED: Test run() executes binary with policy
def test_seatbelt_runner_run_success():
    """run() executes mxc-exec-mac and returns SandboxResult."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.mac import SeatbeltRunner
    
    policy = SandboxPolicy(
        backend="seatbelt",
        command_line="echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )
    
    # Mock subprocess.run to simulate successful execution
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "test output"
        mock_run.return_value.stderr = ""
        
        runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
        result = runner.run(policy)
        
        assert result.exit_code == 0
        assert result.stdout == "test output"
        assert result.stderr == ""
        assert result.timed_out is False
        assert result.duration_ms >= 0


# RED: Test run() handles nonzero exit code
def test_seatbelt_runner_run_nonzero_exit():
    """run() returns SandboxResult with nonzero exit for failures."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.mac import SeatbeltRunner
    
    policy = SandboxPolicy(
        backend="seatbelt",
        command_line="exit 1",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "error message"
        
        runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
        result = runner.run(policy)
        
        assert result.exit_code == 1
        assert result.stderr == "error message"


# RED: Test run() detects timeout
def test_seatbelt_runner_run_timeout():
    """run() raises SandboxTimeoutError on timeout."""
    from entrabot.sandbox.base import SandboxPolicy, SandboxTimeoutError
    from entrabot.sandbox.mac import SeatbeltRunner
    
    policy = SandboxPolicy(
        backend="seatbelt",
        command_line="sleep 100",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=100,  # Very short timeout
    )
    
    with patch("subprocess.run") as mock_run:
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sleep 100", timeout=0.1)
        
        runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
        
        with pytest.raises(SandboxTimeoutError, match="timeout"):
            runner.run(policy)


# RED: Test run() passes config via stdin
def test_seatbelt_runner_passes_config_via_stdin():
    """run() passes MXC JSON config via stdin, not argv."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.mac import SeatbeltRunner
    
    policy = SandboxPolicy(
        backend="seatbelt",
        command_line="python test.py",
        readonly_paths=["/src"],
        readwrite_paths=["/tmp/output"],
        timeout_ms=30000,
    )
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        
        runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
        runner.run(policy)
        
        # Verify subprocess.run was called with input= (stdin)
        call_kwargs = mock_run.call_args[1]
        assert "input" in call_kwargs
        
        # Verify input is valid JSON
        config = json.loads(call_kwargs["input"])
        assert config["version"] == "0.6.0-alpha"
        assert config["containment"] == "seatbelt"


# RED: Test run() passes --experimental flag
def test_seatbelt_runner_passes_experimental_flag():
    """run() passes --experimental flag (required for macOS)."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.mac import SeatbeltRunner
    
    policy = SandboxPolicy(
        backend="seatbelt",
        command_line="echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        
        runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
        runner.run(policy)
        
        # Verify --experimental flag was passed
        call_args = mock_run.call_args[0][0]
        assert "--experimental" in call_args


# RED: Test identity_binding is no-op in Phase 1
def test_seatbelt_runner_identity_binding_noop():
    """identity_binding() is a no-op in Phase 1."""
    from entrabot.sandbox.mac import SeatbeltRunner
    
    runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
    
    # Should not raise
    runner.identity_binding("agent-id-12345")


# RED: Test run() measures duration
def test_seatbelt_runner_measures_duration():
    """run() measures execution duration in milliseconds."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.mac import SeatbeltRunner
    
    policy = SandboxPolicy(
        backend="seatbelt",
        command_line="echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "test"
        mock_run.return_value.stderr = ""
        
        with patch("time.time") as mock_time:
            # Simulate 123ms execution
            mock_time.side_effect = [1000.0, 1000.123]
            
            runner = SeatbeltRunner(binary_path="/fake/mxc-exec-mac")
            result = runner.run(policy)
            
            # Duration should be ~123ms
            assert result.duration_ms == 123
