"""Tests for sandbox/windows.py — Windows MXC process-container runner."""

import base64
import json
import subprocess
from unittest.mock import patch

import pytest


def _make_policy(command_line="cmd /c echo test", timeout_ms=5000, **kwargs):
    from entrabot.sandbox.base import SandboxPolicy

    return SandboxPolicy(
        backend="process",
        command_line=command_line,
        readonly_paths=kwargs.get("readonly_paths", []),
        readwrite_paths=kwargs.get("readwrite_paths", []),
        timeout_ms=timeout_ms,
    )


def _stub_proc(mock_popen, *, stdout="", stderr="", returncode=0, pid=4242):
    """Configure the mocked ``subprocess.Popen`` instance for a normal run."""
    proc = mock_popen.return_value
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = pid
    return proc


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
    from entrabot.sandbox.windows import ProcessContainerRunner

    with patch("subprocess.Popen") as mock_popen:
        _stub_proc(mock_popen, stdout="test output")

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        result = runner.run(_make_policy())

        assert result.exit_code == 0
        assert result.stdout == "test output"
        assert result.stderr == ""
        assert result.timed_out is False
        assert result.duration_ms >= 0


def test_process_container_runner_run_nonzero_exit():
    """run() returns SandboxResult with nonzero exit for failures (e.g. denied)."""
    from entrabot.sandbox.windows import ProcessContainerRunner

    with patch("subprocess.Popen") as mock_popen:
        _stub_proc(mock_popen, stderr="Access is denied.", returncode=1)

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        result = runner.run(_make_policy(command_line="cmd /c exit 1"))

        assert result.exit_code == 1
        assert result.stderr == "Access is denied."


def test_process_container_runner_run_timeout():
    """run() raises SandboxTimeoutError on timeout."""
    from entrabot.sandbox.base import SandboxTimeoutError
    from entrabot.sandbox.windows import ProcessContainerRunner

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("subprocess.run"),  # taskkill in the timeout path
    ):
        proc = mock_popen.return_value
        proc.pid = 4242
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="wxc-exec.exe", timeout=0.1),
            ("", ""),  # post-kill drain
        ]

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")

        with pytest.raises(SandboxTimeoutError, match="timeout"):
            runner.run(_make_policy(command_line="cmd /c timeout 100", timeout_ms=100))


def test_process_container_runner_timeout_kills_process_tree():
    """On timeout the runner force-kills the whole wxc-exec.exe process TREE.

    ``Popen.kill()``/TerminateProcess reaches only the direct child.
    ``wxc-exec.exe`` spawns the container host as a grandchild that inherits
    the stdout/stderr pipe handles, so killing the direct child alone leaves
    the pipes open and a naive drain blocks for as long as the orphan lives
    (observed live 2026-07-02: a 26-minute hang on a 30s timeout). The runner
    must ``taskkill /T /F`` the tree before draining.
    """
    from entrabot.sandbox.base import SandboxTimeoutError
    from entrabot.sandbox.windows import ProcessContainerRunner

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("subprocess.run") as mock_taskkill,
    ):
        proc = mock_popen.return_value
        proc.pid = 31337
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="wxc-exec.exe", timeout=0.1),
            ("", ""),  # post-kill drain returns once the tree is dead
        ]

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        with pytest.raises(SandboxTimeoutError):
            runner.run(_make_policy(timeout_ms=100))

        assert mock_taskkill.called
        taskkill_argv = mock_taskkill.call_args[0][0]
        assert taskkill_argv[0] == "taskkill"
        assert "/T" in taskkill_argv
        assert "/F" in taskkill_argv
        assert "31337" in taskkill_argv


def test_process_container_runner_timeout_drain_is_bounded():
    """The post-kill pipe drain runs with a timeout of its own.

    If a descendant survives the tree-kill and keeps a pipe handle open, the
    drain must give up after a bounded wait and still raise
    SandboxTimeoutError — never block the MCP tool call indefinitely.
    """
    from entrabot.sandbox.base import SandboxTimeoutError
    from entrabot.sandbox.windows import ProcessContainerRunner

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("subprocess.run"),
    ):
        proc = mock_popen.return_value
        proc.pid = 4242
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="wxc-exec.exe", timeout=0.1),
            subprocess.TimeoutExpired(cmd="wxc-exec.exe", timeout=5.0),
        ]

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        with pytest.raises(SandboxTimeoutError, match="timeout"):
            runner.run(_make_policy(timeout_ms=100))

        # Both communicate calls carried an explicit timeout bound.
        for call in proc.communicate.call_args_list:
            assert call.kwargs.get("timeout") is not None


def test_process_container_runner_passes_config_via_base64():
    """run() passes MXC JSON config via --config-base64, not stdin."""
    from entrabot.sandbox.windows import ProcessContainerRunner

    with patch("subprocess.Popen") as mock_popen:
        _stub_proc(mock_popen)

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        runner.run(
            _make_policy(
                command_line="cmd /c echo hi",
                timeout_ms=30000,
                readonly_paths=["C:\\src"],
                readwrite_paths=["C:\\out"],
            )
        )

        # Config is delivered as a positional --config-base64 argument, NOT stdin.
        call_args = mock_popen.call_args[0][0]
        call_kwargs = mock_popen.call_args[1]
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
    from entrabot.sandbox.windows import ProcessContainerRunner

    with patch("subprocess.Popen") as mock_popen:
        _stub_proc(mock_popen)

        runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
        runner.run(_make_policy())

        call_args = mock_popen.call_args[0][0]
        assert "--experimental" not in call_args


def test_process_container_runner_identity_binding_noop():
    """identity_binding() is a no-op in Phase 1."""
    from entrabot.sandbox.windows import ProcessContainerRunner

    runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")

    # Should not raise
    runner.identity_binding("agent-id-12345")


def test_process_container_runner_measures_duration():
    """run() measures execution duration in milliseconds."""
    from entrabot.sandbox.windows import ProcessContainerRunner

    with patch("subprocess.Popen") as mock_popen:
        _stub_proc(mock_popen, stdout="test")

        with patch("time.time") as mock_time:
            mock_time.side_effect = [1000.0, 1000.123]

            runner = ProcessContainerRunner(binary_path="C:\\fake\\wxc-exec.exe")
            result = runner.run(_make_policy())

            assert result.duration_ms == 123
