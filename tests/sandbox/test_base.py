"""Tests for sandbox/base.py — protocol, dataclasses, errors."""



# RED: Test Backend enum exists and has expected values
def test_backend_enum_has_process():
    """Backend enum includes PROCESS for Phase 1 process isolation."""
    from entrabot.sandbox.base import Backend
    
    assert hasattr(Backend, "PROCESS")
    assert Backend.PROCESS.value == "process"


def test_backend_enum_has_session_stub():
    """Backend enum includes SESSION for Phase 2 (stub, not implemented)."""
    from entrabot.sandbox.base import Backend
    
    assert hasattr(Backend, "SESSION")
    assert Backend.SESSION.value == "session"


# RED: Test SandboxPolicy dataclass
def test_sandbox_policy_dataclass_exists():
    """SandboxPolicy dataclass can be instantiated with required fields."""
    from entrabot.sandbox.base import SandboxPolicy
    
    policy = SandboxPolicy(
        backend="process",
        command_line="python test.py",
        readonly_paths=["/src"],
        readwrite_paths=["/tmp/output"],
        timeout_ms=30000,
    )
    
    assert policy.backend == "process"
    assert policy.command_line == "python test.py"
    assert policy.readonly_paths == ["/src"]
    assert policy.readwrite_paths == ["/tmp/output"]
    assert policy.timeout_ms == 30000


def test_sandbox_policy_has_network_defaults():
    """SandboxPolicy has network_default_policy with default 'block'."""
    from entrabot.sandbox.base import SandboxPolicy
    
    policy = SandboxPolicy(
        backend="process",
        command_line="echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )
    
    # Default should be 'block' for defense-in-depth
    assert policy.network_default_policy == "block"


def test_sandbox_policy_has_keychain_access_false():
    """SandboxPolicy has keychain_access hardcoded to False (Phase 1)."""
    from entrabot.sandbox.base import SandboxPolicy
    
    policy = SandboxPolicy(
        backend="process",
        command_line="echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
    )
    
    assert policy.keychain_access is False


# RED: Test SandboxResult dataclass
def test_sandbox_result_success():
    """SandboxResult captures stdout, stderr, exit code for successful run."""
    from entrabot.sandbox.base import SandboxResult
    
    result = SandboxResult(
        exit_code=0,
        stdout="output",
        stderr="",
        duration_ms=123,
        timed_out=False,
    )
    
    assert result.exit_code == 0
    assert result.stdout == "output"
    assert result.stderr == ""
    assert result.duration_ms == 123
    assert result.timed_out is False


def test_sandbox_result_failure():
    """SandboxResult captures nonzero exit and stderr for failures."""
    from entrabot.sandbox.base import SandboxResult
    
    result = SandboxResult(
        exit_code=1,
        stdout="",
        stderr="Error: command failed",
        duration_ms=50,
        timed_out=False,
    )
    
    assert result.exit_code == 1
    assert result.stderr == "Error: command failed"


def test_sandbox_result_timeout():
    """SandboxResult marks timeouts with timed_out=True."""
    from entrabot.sandbox.base import SandboxResult
    
    result = SandboxResult(
        exit_code=124,  # Common timeout exit code
        stdout="partial",
        stderr="Killed: timeout",
        duration_ms=30000,
        timed_out=True,
    )
    
    assert result.timed_out is True
    assert result.duration_ms == 30000


# RED: Test error taxonomy
def test_sandbox_unavailable_error_exists():
    """SandboxUnavailableError raised when binary not found."""
    from entrabot.sandbox.base import SandboxUnavailableError
    
    error = SandboxUnavailableError("mxc-exec-mac not found")
    assert "not found" in str(error)


def test_sandbox_untrusted_binary_error_exists():
    """SandboxUntrustedBinaryError raised when SHA256 verification fails."""
    from entrabot.sandbox.base import SandboxUntrustedBinaryError
    
    error = SandboxUntrustedBinaryError("SHA256 mismatch: expected abc, got def")
    assert "mismatch" in str(error)


def test_sandbox_backend_unsupported_error_exists():
    """SandboxBackendUnsupportedError raised when policy needs unenforceable primitive."""
    from entrabot.sandbox.base import SandboxBackendUnsupportedError
    
    error = SandboxBackendUnsupportedError(
        "allowedHosts not supported on macOS Seatbelt backend"
    )
    assert "not supported" in str(error)


def test_sandbox_policy_error_exists():
    """SandboxPolicyError raised for ceiling violations or invalid schema."""
    from entrabot.sandbox.base import SandboxPolicyError
    
    error = SandboxPolicyError("Policy exceeds operator ceiling")
    assert "ceiling" in str(error)


def test_sandbox_execution_error_exists():
    """SandboxExecutionError raised when sandbox process crashes."""
    from entrabot.sandbox.base import SandboxExecutionError
    
    error = SandboxExecutionError("Process crashed with signal 11")
    assert "crashed" in str(error)


def test_sandbox_timeout_error_exists():
    """SandboxTimeoutError raised when execution exceeds timeout."""
    from entrabot.sandbox.base import SandboxTimeoutError
    
    error = SandboxTimeoutError("Execution exceeded 30000ms timeout")
    assert "timeout" in str(error)


# RED: Test SandboxRunner protocol
def test_sandbox_runner_protocol_exists():
    """SandboxRunner protocol defines run() and get_capabilities()."""
    from entrabot.sandbox.base import SandboxRunner
    
    # Protocol should be a class (Protocol base)
    assert hasattr(SandboxRunner, "__mro__")


def test_sandbox_runner_protocol_has_run_method():
    """SandboxRunner protocol requires run(policy) -> SandboxResult."""
    import inspect

    from entrabot.sandbox.base import SandboxRunner
    
    # Check run method exists in protocol
    assert hasattr(SandboxRunner, "run")
    # Protocol methods have annotations
    sig = inspect.signature(SandboxRunner.run)
    assert "policy" in sig.parameters


def test_sandbox_runner_protocol_has_get_capabilities():
    """SandboxRunner protocol requires get_capabilities() -> dict."""
    import inspect

    from entrabot.sandbox.base import SandboxRunner
    
    assert hasattr(SandboxRunner, "get_capabilities")
    sig = inspect.signature(SandboxRunner.get_capabilities)
    # Should return dict of capabilities
    assert sig.return_annotation is dict or "dict" in str(sig.return_annotation)


def test_sandbox_runner_protocol_has_identity_binding_seam():
    """SandboxRunner protocol has identity_binding() seam (no-op in Phase 1)."""
    from entrabot.sandbox.base import SandboxRunner
    
    assert hasattr(SandboxRunner, "identity_binding")


# RED: Test concrete runner implementation check
def test_concrete_runner_must_implement_protocol():
    """Concrete SandboxRunner must implement all protocol methods."""
    from entrabot.sandbox.base import SandboxPolicy, SandboxResult, SandboxRunner
    
    # Define a minimal concrete runner
    class TestRunner:
        def run(self, policy: SandboxPolicy) -> SandboxResult:
            return SandboxResult(
                exit_code=0, stdout="", stderr="", duration_ms=0, timed_out=False
            )
        
        def get_capabilities(self) -> dict:
            return {"backend": "test", "network_filtering": False}
        
        def identity_binding(self, agent_identity: str) -> None:
            pass  # No-op in Phase 1
    
    runner: SandboxRunner = TestRunner()
    assert runner.get_capabilities()["backend"] == "test"
