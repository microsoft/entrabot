# entrabot.sandbox — MXC execution-container integration

import sys

from entrabot.sandbox.base import (
    SandboxRunner,
    SandboxUnavailableError,
)
from entrabot.sandbox.binary import resolve_and_verify


def get_sandbox_runner() -> SandboxRunner:
    """Get platform-specific sandbox runner with verified binary.
    
    Returns:
        SandboxRunner for current platform (SeatbeltRunner on macOS)
    
    Raises:
        SandboxUnavailableError: No binary found or platform unsupported
        SandboxUntrustedBinaryError: Binary SHA256 mismatch
    """
    # Resolve and verify binary for current platform
    binary_path = resolve_and_verify()
    
    # Import and instantiate platform-specific runner
    if sys.platform == "darwin":
        from entrabot.sandbox.mac import SeatbeltRunner
        return SeatbeltRunner(binary_path)
    elif sys.platform == "win32":
        # TODO: Windows runner (T4)
        raise SandboxUnavailableError("Windows ProcessContainer runner not yet implemented")
    else:
        # TODO: Linux runner (T10, optional)
        raise SandboxUnavailableError(f"Sandbox not supported on platform: {sys.platform}")

