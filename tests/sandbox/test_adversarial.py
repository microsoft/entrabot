"""
Adversarial integration tests for MXC sandbox.

These tests verify the sandbox withstands real-world attack scenarios:
- Symlink escapes (symlink to protected directory)
- Path traversal (../../.ssh/id_rsa)
- Secret access (keychain, env vars, SSH keys)
- Network exfiltration attempts
- Process tree timeout enforcement
- Binary tampering detection

SECURITY: These tests are OPT-IN via ENTRABOT_TEST_ADVERSARIAL=1.
They create real files, symlinks, and processes to validate containment.
Never run in CI without isolation (use ephemeral containers).
"""

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from entrabot.sandbox import get_sandbox_runner
from entrabot.sandbox.base import Backend, SandboxPolicy, SandboxUnavailableError
from entrabot.sandbox.binary import resolve_and_verify

# Skip all tests unless ENTRABOT_TEST_ADVERSARIAL=1 is set
pytestmark = pytest.mark.skipif(
    os.getenv("ENTRABOT_TEST_ADVERSARIAL") != "1",
    reason="Adversarial tests require ENTRABOT_TEST_ADVERSARIAL=1 (creates real files/processes)",
)


@pytest.fixture
def sandbox_runner():
    """Get platform-specific sandbox runner (requires MXC binary)."""
    try:
        return get_sandbox_runner()
    except SandboxUnavailableError:
        pytest.skip("MXC binary not available for adversarial tests")


@pytest.fixture
def temp_sandbox_dir():
    """Create temporary directory for sandboxed operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestSymlinkEscape:
    """Verify sandbox blocks symlink-based directory traversal."""

    def test_symlink_to_protected_dir_blocked(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should block reads through symlink to protected directory."""
        # Create symlink: /tmp/sandbox/link -> /Users/you/.ssh
        link_path = temp_sandbox_dir / "ssh_link"
        target_dir = Path.home() / ".ssh"
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "test_secret.txt").write_text("SECRET")

        link_path.symlink_to(target_dir)

        # Try to read through symlink (should fail)
        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["cat", str(link_path / "test_secret.txt")],
            readonly_paths=[str(temp_sandbox_dir)],  # Only /tmp/sandbox allowed
            readwrite_paths=[],
            timeout_ms=5000,
            network_policy="block",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should fail (symlink target not in allowlist)
        assert result.exit_code != 0, "Symlink escape should be blocked"
        assert "SECRET" not in result.stdout, "Should not read through symlink"

    def test_symlink_within_allowed_dir_permitted(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should allow symlinks that stay within allowlist."""
        # Create symlink within allowed directory
        file_path = temp_sandbox_dir / "real_file.txt"
        link_path = temp_sandbox_dir / "link.txt"
        file_path.write_text("allowed content")
        link_path.symlink_to(file_path)

        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["cat", str(link_path)],
            readonly_paths=[str(temp_sandbox_dir)],
            readwrite_paths=[],
            timeout_ms=5000,
            network_policy="block",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should succeed (symlink target in allowlist)
        assert result.exit_code == 0, "Symlink within allowlist should work"
        assert "allowed content" in result.stdout


class TestPathTraversal:
    """Verify sandbox blocks path traversal attacks."""

    def test_path_traversal_blocked(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should block ../../ path traversal."""
        # Try to read outside sandbox via path traversal
        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["cat", f"{temp_sandbox_dir}/../../etc/passwd"],
            readonly_paths=[str(temp_sandbox_dir)],
            readwrite_paths=[],
            timeout_ms=5000,
            network_policy="block",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should fail (path traverses outside allowlist)
        assert result.exit_code != 0, "Path traversal should be blocked"
        assert "root:" not in result.stdout, "Should not read /etc/passwd"

    def test_absolute_path_outside_allowlist_blocked(self, sandbox_runner):
        """Sandbox should block absolute paths outside allowlist."""
        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["cat", str(Path.home() / ".ssh/id_rsa")],
            readonly_paths=["/tmp"],  # Only /tmp allowed
            readwrite_paths=[],
            timeout_ms=5000,
            network_policy="block",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should fail (absolute path not in allowlist)
        assert result.exit_code != 0, "Absolute path outside allowlist should fail"


class TestSecretAccess:
    """Verify sandbox blocks access to secrets (keychain, env, SSH keys)."""

    def test_keychain_access_denied(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should block keychain access (keychainAccess=false hardcoded)."""
        # Try to access macOS keychain (will fail on macOS, no-op on Linux/Windows)
        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["security", "find-generic-password", "-s", "test"],
            readonly_paths=[str(temp_sandbox_dir)],
            readwrite_paths=[],
            timeout_ms=5000,
            network_policy="block",
            keychain_access=False,  # Hardcoded in policy builder
        )

        result = sandbox_runner.run(policy)

        # Should fail (keychain blocked)
        # Exit code may vary (command not found on Linux, denied on macOS)
        # Key check: no keychain data in output
        assert "password:" not in result.stdout.lower()

    def test_ssh_key_access_blocked(self, sandbox_runner):
        """Sandbox should block reads of SSH private keys."""
        ssh_dir = Path.home() / ".ssh"
        if not ssh_dir.exists() or not (ssh_dir / "id_rsa").exists():
            pytest.skip("No SSH keys to test against")

        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["cat", str(ssh_dir / "id_rsa")],
            readonly_paths=["/tmp"],  # SSH dir not in allowlist
            readwrite_paths=[],
            timeout_ms=5000,
            network_policy="block",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should fail (SSH key not in allowlist)
        assert result.exit_code != 0
        assert "BEGIN PRIVATE KEY" not in result.stdout
        assert "BEGIN RSA PRIVATE KEY" not in result.stdout

    def test_environment_variable_isolation(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should not expose sensitive env vars to subprocess."""
        # Set sensitive env var in test process
        with patch.dict(os.environ, {"SECRET_TOKEN": "super_secret_value"}):
            policy = SandboxPolicy(
                backend=Backend.PROCESS,
                argv=["sh", "-c", "echo $SECRET_TOKEN"],
                readonly_paths=[str(temp_sandbox_dir)],
                readwrite_paths=[],
                timeout_ms=5000,
                network_policy="block",
                keychain_access=False,
            )

            result = sandbox_runner.run(policy)

            # Env var should not leak into sandbox
            # (MXC may or may not inherit env — test documents expectation)
            # For now, just check it doesn't echo the secret
            if "super_secret_value" in result.stdout:
                pytest.fail("Sandbox leaked SECRET_TOKEN env var")


class TestNetworkIsolation:
    """Verify sandbox enforces network isolation."""

    def test_network_block_enforced(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should block network access when defaultPolicy=block."""
        # Try to make network request (curl or wget)
        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["curl", "-s", "--max-time", "2", "https://example.com"],
            readonly_paths=[str(temp_sandbox_dir)],
            readwrite_paths=[],
            timeout_ms=5000,
            network_policy="block",  # No network allowed
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should fail (network blocked)
        assert result.exit_code != 0, "Network access should be blocked"
        assert result.duration_ms < 5000, "Should fail quickly, not timeout"

    @pytest.mark.skip(reason="Network allow not yet implemented in test mock")
    def test_network_allow_succeeds(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should allow network when defaultPolicy=allow."""
        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["curl", "-s", "--max-time", "2", "https://example.com"],
            readonly_paths=[str(temp_sandbox_dir)],
            readwrite_paths=[],
            timeout_ms=5000,
            network_policy="allow",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should succeed (network allowed)
        assert result.exit_code == 0


class TestTimeoutEnforcement:
    """Verify sandbox enforces timeout and kills process tree."""

    def test_timeout_kills_process(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should kill process that exceeds timeout."""
        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["sleep", "30"],  # Sleep longer than timeout
            readonly_paths=[str(temp_sandbox_dir)],
            readwrite_paths=[],
            timeout_ms=1000,  # 1 second timeout
            network_policy="block",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should be killed by timeout
        assert result.duration_ms >= 1000, "Should run for at least timeout duration"
        assert result.duration_ms < 5000, "Should be killed promptly after timeout"
        # Exit code varies (SIGTERM = 143, SIGKILL = 137)
        assert result.exit_code != 0, "Timeout should result in non-zero exit"

    def test_timeout_kills_process_tree(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should kill entire process tree on timeout."""
        # Start process that spawns children
        script_path = temp_sandbox_dir / "spawn_children.sh"
        script_path.write_text(
            """#!/bin/bash
            sleep 30 &
            sleep 30 &
            sleep 30
            """
        )
        script_path.chmod(0o755)

        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=[str(script_path)],
            readonly_paths=[str(temp_sandbox_dir)],
            readwrite_paths=[],
            timeout_ms=1000,
            network_policy="block",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should be killed by timeout
        assert result.exit_code != 0

        # Verify no zombie sleep processes remain
        # (This is a best-effort check — may not catch all zombies)
        try:
            ps_result = subprocess.run(
                ["pgrep", "-f", "spawn_children.sh"],
                capture_output=True,
                timeout=2,
            )
            assert ps_result.returncode != 0, "No spawn_children processes should remain"
        except subprocess.TimeoutExpired:
            pytest.fail("pgrep timed out — possible zombie processes")


class TestBinaryTampering:
    """Verify sandbox detects and blocks tampered binaries."""

    def test_tampered_binary_detected(self, temp_sandbox_dir):
        """resolve_and_verify() should reject binary with wrong SHA256."""
        # Create fake binary
        fake_binary = temp_sandbox_dir / "mxc-exec-mac"
        fake_binary.write_bytes(b"fake tampered binary")
        fake_binary.chmod(0o755)

        # Try to verify (should fail - hash mismatch)
        with patch.dict(
            os.environ, {"MXC_BIN_DIR": str(temp_sandbox_dir)}
        ), pytest.raises(Exception, match="SHA256"):
            # PINNED_HASHES won't match our fake binary
            resolve_and_verify(platform_name="darwin", arch="arm64")

    def test_binary_verification_mandatory(self, temp_sandbox_dir):
        """Binary verification cannot be bypassed."""
        # Verify resolve_and_verify always checks SHA256
        fake_binary = temp_sandbox_dir / "mxc-exec-mac"
        fake_binary.write_bytes(b"different content")
        fake_binary.chmod(0o755)

        # Compute actual hash
        actual_hash = hashlib.sha256(b"different content").hexdigest()

        # Patch PINNED_HASHES to match
        from entrabot.sandbox import binary as binary_module

        original_hashes = binary_module.PINNED_HASHES.copy()
        try:
            binary_module.PINNED_HASHES["darwin-arm64"] = actual_hash

            with patch.dict(os.environ, {"MXC_BIN_DIR": str(temp_sandbox_dir)}):
                # Should succeed (hash matches)
                binary_path = resolve_and_verify(platform_name="darwin", arch="arm64")
                assert binary_path is not None
                assert binary_path == fake_binary
        finally:
            binary_module.PINNED_HASHES = original_hashes


class TestForkBomb:
    """Verify sandbox limits process spawning (future: process limit enforcement)."""

    @pytest.mark.skip(reason="Process limit not enforced in Phase 1")
    def test_fork_bomb_contained(self, sandbox_runner, temp_sandbox_dir):
        """Sandbox should limit process spawning to prevent fork bombs."""
        # Fork bomb: :(){ :|:& };:
        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=["bash", "-c", ":(){ :|:& };:"],
            readonly_paths=[str(temp_sandbox_dir)],
            readwrite_paths=[],
            timeout_ms=2000,
            network_policy="block",
            keychain_access=False,
        )

        result = sandbox_runner.run(policy)

        # Should be killed/limited (doesn't crash the test runner)
        assert result.exit_code != 0
        # Test passes if we reach here (fork bomb didn't hang the test)


class TestWriteAfterSandboxExit:
    """Verify sandbox cleanup prevents writes after process exit."""

    def test_no_background_writes_after_exit(self, sandbox_runner, temp_sandbox_dir):
        """Background processes should not write after sandbox timeout."""
        # Start script that writes in background loop
        script_path = temp_sandbox_dir / "background_writer.sh"
        output_file = temp_sandbox_dir / "output.txt"
        script_path.write_text(
            f"""#!/bin/bash
            while true; do
                echo "still writing" >> {output_file}
                sleep 0.1
            done
            """
        )
        script_path.chmod(0o755)

        policy = SandboxPolicy(
            backend=Backend.PROCESS,
            argv=[str(script_path)],
            readonly_paths=[],
            readwrite_paths=[str(temp_sandbox_dir)],
            timeout_ms=500,  # Kill after 500ms
            network_policy="block",
            keychain_access=False,
        )

        _ = sandbox_runner.run(policy)  # Run to trigger side effects

        # Count lines written before timeout
        if output_file.exists():
            lines_during = len(output_file.read_text().strip().split("\n"))
        else:
            lines_during = 0

        # Wait 1 second, check if more lines appeared (should not)
        import time

        time.sleep(1)

        if output_file.exists():
            lines_after = len(output_file.read_text().strip().split("\n"))
        else:
            lines_after = 0

        # Should not have written more after timeout
        assert (
            lines_after == lines_during
        ), "No writes should occur after sandbox timeout"
