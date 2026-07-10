"""
Sandbox base module — protocol, dataclasses, error taxonomy.

MXC (Microsoft Execution Containers) integration for contained local code execution.
Design: docs/architecture/DESIGN-mxc-sandbox.md
Platform research: docs/platform-learnings/mxc-windows-sandbox.md
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class Backend(Enum):
    """Sandbox backend enumeration.
    
    PROCESS: Phase 1 process isolation (macOS Seatbelt, Windows processcontainer)
    SESSION: Phase 2 Entra-bound session isolation (stub, not implemented)
    """
    PROCESS = "process"
    SESSION = "session"


@dataclass
class SandboxPolicy:
    """Sandbox policy configuration.
    
    Positive-allowlist-only design (no reliance on deniedPaths).
    All paths are canonicalized and symlinks validated server-side.
    """
    backend: str
    command_line: str
    readonly_paths: list[str]
    readwrite_paths: list[str]
    timeout_ms: int
    network_default_policy: str = "block"  # 'block' or 'allow'
    keychain_access: bool = False  # Hardcoded False in Phase 1, not overridable
    allowed_hosts: list[str] = field(default_factory=list)  # Best-effort on macOS
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxResult:
    """Result of sandbox execution."""
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


# Error taxonomy
class SandboxUnavailableError(Exception):
    """Raised when sandbox binary not found (no MXC installed)."""
    pass


class SandboxUntrustedBinaryError(Exception):
    """Raised when binary SHA256 verification fails."""
    pass


class SandboxBackendUnsupportedError(Exception):
    """Raised when policy needs a primitive the backend cannot enforce (fail-closed)."""
    pass


class SandboxPolicyError(Exception):
    """Raised for ceiling violations or invalid policy schema."""
    pass


class SandboxExecutionError(Exception):
    """Raised when sandbox process crashes or returns nonzero."""
    pass


class SandboxTimeoutError(Exception):
    """Raised when execution exceeds timeout."""
    pass


class SandboxRunner(Protocol):
    """Protocol for platform-specific sandbox runners.
    
    Implementers: mac.py (SeatbeltRunner), windows.py (ProcessContainerRunner)
    """
    
    def run(self, policy: SandboxPolicy) -> SandboxResult:
        """Execute command in sandbox with given policy.
        
        Raises:
            SandboxExecutionError: Process crashed or failed
            SandboxTimeoutError: Execution exceeded timeout
            SandboxBackendUnsupportedError: Policy cannot be enforced
        """
        ...
    
    def get_capabilities(self) -> dict:
        """Return backend capabilities dict.
        
        Returns dict with:
            - backend: str (e.g., 'seatbelt', 'processcontainer')
            - network_filtering: bool (whether allowedHosts is enforceable)
            - deny_paths_supported: bool (whether deniedPaths works)
        """
        ...
    
    def identity_binding(self, agent_identity: str) -> None:
        """Phase 2 seam: bind sandbox session to Entra agent identity.
        
        No-op in Phase 1 (process isolation).
        Phase 2: attaches agent_identity to session isolation backend.
        """
        ...
