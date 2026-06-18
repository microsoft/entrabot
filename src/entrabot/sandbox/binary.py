"""
Binary resolution and verification for MXC executables.

Three-tier resolution strategy:
1. Prebuilt binary in MXC_BIN_DIR (verified against pinned SHA256)
2. npm global bin (@microsoft/mxc-sdk)
3. None → SandboxUnavailableError

All binaries are SHA256-verified before use.
"""

import hashlib
import os
import subprocess
from pathlib import Path

from entrabot.sandbox.base import (
    SandboxUnavailableError,
    SandboxUntrustedBinaryError,
)

# Pinned SHA256 hashes for MXC binaries (commit-pinned, verified)
# These will be populated by setup_sandbox.sh after building/downloading
# For now, stub with placeholders (real hashes added after binary acquisition)
PINNED_HASHES: dict[str, str] = {
    "darwin-arm64": "0000000000000000000000000000000000000000000000000000000000000000",
    "darwin-x86_64": "0000000000000000000000000000000000000000000000000000000000000000",
    "win32-x86_64": "0000000000000000000000000000000000000000000000000000000000000000",
    "win32-amd64": "0000000000000000000000000000000000000000000000000000000000000000",
    "linux-x86_64": "0000000000000000000000000000000000000000000000000000000000000000",
}


def get_binary_name(platform_name: str) -> str:
    """Get platform-specific MXC binary name.
    
    Args:
        platform_name: sys.platform value ('darwin', 'win32', 'linux')
    
    Returns:
        Binary filename for the platform
    """
    if platform_name == "darwin":
        return "mxc-exec-mac"
    elif platform_name == "win32":
        return "wxc-exec.exe"
    else:  # linux and others
        return "lxc-exec"


def resolve_binary(
    platform: str | None = None,
    arch: str | None = None,
) -> str | None:
    """Resolve MXC binary path (prebuilt or npm).
    
    Resolution order:
    1. MXC_BIN_DIR env var: <dir>/<arch>/<binary>
    2. npm global bin: $(npm bin -g)/@microsoft/mxc-sdk/bin/<binary>
    3. None
    
    Args:
        platform: Platform name (defaults to sys.platform)
        arch: Architecture (defaults to platform.machine())
    
    Returns:
        Absolute path to binary, or None if not found
    """
    if platform is None:
        import sys
        platform = sys.platform
    
    if arch is None:
        arch = platform.machine()
    
    binary_name = get_binary_name(platform)
    
    # 1. Check MXC_BIN_DIR
    mxc_bin_dir = os.environ.get("MXC_BIN_DIR")
    if mxc_bin_dir:
        bin_path = Path(mxc_bin_dir) / arch / binary_name
        if bin_path.exists():
            return str(bin_path)
    
    # 2. Check npm global bin
    try:
        result = subprocess.run(
            ["npm", "bin", "-g"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            npm_bin = result.stdout.strip()
            npm_path = Path(npm_bin) / binary_name
            if npm_path.exists():
                return str(npm_path)
            
            # Also try the @microsoft/mxc-sdk structure
            sdk_path = (
                Path(npm_bin).parent
                / "node_modules"
                / "@microsoft"
                / "mxc-sdk"
                / "bin"
                / binary_name
            )
            if sdk_path.exists():
                return str(sdk_path)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    
    # 3. Not found
    return None


def verify_binary(binary_path: str, expected_hash: str) -> None:
    """Verify binary SHA256 matches expected hash.
    
    Args:
        binary_path: Path to binary
        expected_hash: Expected SHA256 hex digest
    
    Raises:
        SandboxUntrustedBinaryError: Hash mismatch or file not found
    """
    if not Path(binary_path).exists():
        raise SandboxUntrustedBinaryError(f"Binary not found: {binary_path}")
    
    # Compute SHA256
    sha256 = hashlib.sha256()
    with open(binary_path, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    
    actual_hash = sha256.hexdigest()
    
    if actual_hash != expected_hash:
        raise SandboxUntrustedBinaryError(
            f"SHA256 mismatch for {binary_path}: expected {expected_hash}, got {actual_hash}"
        )


def resolve_and_verify(
    platform: str | None = None,
    arch: str | None = None,
) -> str:
    """Resolve and verify MXC binary.
    
    Combines resolve_binary() + verify_binary() with pinned hash lookup.
    
    Args:
        platform: Platform name (defaults to sys.platform)
        arch: Architecture (defaults to platform.machine())
    
    Returns:
        Absolute path to verified binary
    
    Raises:
        SandboxUnavailableError: Binary not found
        SandboxUntrustedBinaryError: Binary hash mismatch
    """
    if platform is None:
        import sys
        platform = sys.platform
    
    if arch is None:
        arch = platform.machine()
    
    # Resolve binary
    binary_path = resolve_binary(platform, arch)
    if binary_path is None:
        raise SandboxUnavailableError(
            f"MXC binary not found for {platform}-{arch}. "
            f"Set MXC_BIN_DIR or install @microsoft/mxc-sdk via npm."
        )
    
    # Get expected hash for platform-arch combo
    hash_key = f"{platform}-{arch}"
    expected_hash = PINNED_HASHES.get(hash_key)
    
    if expected_hash is None:
        raise SandboxUnavailableError(
            f"No pinned hash for {hash_key}. Supported: {list(PINNED_HASHES.keys())}"
        )
    
    # Verify hash
    verify_binary(binary_path, expected_hash)
    
    return binary_path
