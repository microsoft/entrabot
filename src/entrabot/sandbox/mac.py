"""
macOS Seatbelt runner for MXC sandbox.

Uses Apple's Seatbelt sandbox (same as Mac App Store App Sandbox).
Backend: seatbelt (process-scoped, no container lifecycle)
Requires: --experimental flag (macOS support is experimental in MXC 0.6.0-alpha)
"""

import subprocess
import time

from entrabot.sandbox.base import (
    SandboxPolicy,
    SandboxResult,
    SandboxTimeoutError,
)
from entrabot.sandbox.policy import build_policy


class SeatbeltRunner:
    """macOS Seatbelt sandbox runner.
    
    Implements SandboxRunner protocol for macOS.
    Uses mxc-exec-mac binary with Seatbelt backend.
    """
    
    def __init__(self, binary_path: str):
        """Initialize with path to mxc-exec-mac binary.
        
        Args:
            binary_path: Absolute path to verified mxc-exec-mac binary
        """
        self.binary_path = binary_path
    
    def run(self, policy: SandboxPolicy) -> SandboxResult:
        """Execute command in Seatbelt sandbox.
        
        Args:
            policy: Sandbox policy configuration
        
        Returns:
            SandboxResult with stdout, stderr, exit code, duration
        
        Raises:
            SandboxTimeoutError: Execution exceeded timeout
        """
        # Build MXC JSON config
        mxc_config = build_policy(policy)
        
        # Build command: mxc-exec-mac --experimental (config via stdin)
        cmd = [
            self.binary_path,
            "--experimental",  # Required for macOS
        ]
        
        # Measure duration
        start_time = time.time()
        
        try:
            # Execute with timeout (convert ms to seconds)
            timeout_seconds = policy.timeout_ms / 1000.0
            
            result = subprocess.run(
                cmd,
                input=mxc_config,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            
            end_time = time.time()
            duration_ms = int((end_time - start_time) * 1000)
            
            return SandboxResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=duration_ms,
                timed_out=False,
            )
            
        except subprocess.TimeoutExpired as e:
            raise SandboxTimeoutError(
                f"Execution exceeded {policy.timeout_ms}ms timeout"
            ) from e
    
    def get_capabilities(self) -> dict:
        """Return Seatbelt backend capabilities.
        
        Returns:
            Dict with backend capabilities:
            - backend: 'seatbelt'
            - network_host_filtering: False (can't filter by DNS)
            - deny_paths_supported: False (not using deniedPaths)
        """
        return {
            "backend": "seatbelt",
            "network_host_filtering": False,  # macOS can't filter by host
            "deny_paths_supported": False,  # Using positive-allowlist only
        }
    
    def identity_binding(self, agent_identity: str) -> None:
        """No-op in Phase 1 (process isolation).
        
        Phase 2: Would bind sandbox to Entra agent identity via session isolation.
        
        Args:
            agent_identity: Entra Agent ID (unused in Phase 1)
        """
        pass  # No-op in Phase 1
