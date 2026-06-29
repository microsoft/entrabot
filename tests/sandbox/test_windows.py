"""Tests for sandbox/windows.py — Windows MXC process-container runner."""

import base64
import json
from unittest.mock import patch

import pytest


def test_process_container_runner_implements_protocol():
    """ProcessContainerRunner implements SandboxRunner protocol."""
    from entrabot.sandbox.windows import ProcessContainerRunner

    runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")

    assert callable(runner.run)
    assert callable(runner.get_capabilities)
    assert callable(runner.identity_binding)


def test_process_container_runner_capabilities():
    """get_capabilities() returns processcontainer backend capabilities."""
    from entrabot.sandbox.windows import ProcessContainerRunner

    runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
    caps = runner.get_capabilities()

    assert caps["backend"] == "processcontainer"
    # allowedHosts is NOT enforced on Windows — fail-closed must see False.
    assert caps["network_host_filtering"] is False
    assert caps["deny_paths_supported"] is False


def test_process_container_runner_run_success():
    """run() executes wxc-exec.exe and returns SandboxResult."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.windows import ProcessContainerRunner

    policy = SandboxPolicy(
        backend="process",
        command_line="cmd /c echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "test output"
        mock_run.return_value.stderr = ""

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        result = runner.run(policy)

        assert result.exit_code == 0
        assert result.stdout == "test output"
        assert result.stderr == ""
        assert result.timed_out is False
        assert result.duration_ms >= 0


def test_process_container_runner_run_nonzero_exit():
    """run() returns SandboxResult with nonzero exit for failures (e.g. denied)."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.windows import ProcessContainerRunner

    policy = SandboxPolicy(
        backend="process",
        command_line="cmd /c exit 1",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "Access is denied."

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        result = runner.run(policy)

        assert result.exit_code == 1
        assert result.stderr == "Access is denied."


def test_process_container_runner_run_timeout():
    """run() raises SandboxTimeoutError on timeout."""
    from entrabot.sandbox.base import SandboxPolicy, SandboxTimeoutError
    from entrabot.sandbox.windows import ProcessContainerRunner

    policy = SandboxPolicy(
        backend="process",
        command_line="cmd /c timeout 100",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=100,
    )

    with patch("subprocess.run") as mock_run:
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="wxc-exec.exe", timeout=0.1)

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")

        with pytest.raises(SandboxTimeoutError, match="timeout"):
            runner.run(policy)


def test_process_container_runner_passes_config_via_base64():
    """run() passes MXC JSON config via --config-base64, not stdin."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.windows import ProcessContainerRunner

    policy = SandboxPolicy(
        backend="process",
        command_line="cmd /c echo hi",
        readonly_paths=["C:\\src"],
        readwrite_paths=["C:\\out"],
        timeout_ms=30000,
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        runner.run(policy)

        # Config is delivered as a positional --config-base64 argument, NOT stdin.
        call_args = mock_run.call_args[0][0]
        call_kwargs = mock_run.call_args[1]
        assert "input" not in call_kwargs  # no stdin path on Windows
        assert "--config-base64" in call_args

        b64 = call_args[call_args.index("--config-base64") + 1]
        config = json.loads(base64.b64decode(b64).decode("utf-8"))
        assert config["version"] == "0.6.0-alpha"
        assert config["containment"] == "process"
        assert "C:\\src" in config["filesystem"]["readonlyPaths"]
        # keychainAccess must NOT be present — the real binary rejects it.
        assert "keychainAccess" not in config


def test_process_container_runner_no_experimental_flag():
    """run() does NOT pass --experimental (processcontainer is a default backend)."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.windows import ProcessContainerRunner

    policy = SandboxPolicy(
        backend="process",
        command_line="cmd /c echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        runner.run(policy)

        call_args = mock_run.call_args[0][0]
        assert "--experimental" not in call_args


def test_process_container_runner_identity_binding_noop():
    """identity_binding() is a no-op in Phase 1."""
    from entrabot.sandbox.windows import ProcessContainerRunner

    runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")

    # Should not raise
    runner.identity_binding("agent-id-12345")


def test_process_container_runner_measures_duration():
    """run() measures execution duration in milliseconds."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.windows import ProcessContainerRunner

    policy = SandboxPolicy(
        backend="process",
        command_line="cmd /c echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "test"
        mock_run.return_value.stderr = ""

        with patch("time.time") as mock_time:
            mock_time.side_effect = [1000.0, 1000.123]

            runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
            result = runner.run(policy)

            assert result.duration_ms == 123
