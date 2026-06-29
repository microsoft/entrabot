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
import platform as _platform_module
import subprocess
from pathlib import Path

from entrabot.sandbox.base import (
    SandboxUnavailableError,
    SandboxUntrustedBinaryError,
)

# Pinned SHA256 hashes for MXC binaries (commit-pinned / release-pinned, verified).
#
# darwin-arm64 is built from microsoft/mxc v0.6.1 (commit
# 161598fd08a4fdd030f461de19af23ce4a310b41) with the local stdin-compat
# patch in scripts/mxc-mac-stdin-compat.patch applied.
#
# win32-arm64 / win32-x64 are the prebuilt ``wxc-exec.exe`` shipped in
# @microsoft/mxc-sdk v0.7.0 (npm), under ``bin/arm64`` and ``bin/x64``. The
# Windows binary is distributed (not built locally), so the pin is taken
# directly from the published package.
#
# Hash keys are ``<sys.platform>-<normalized-arch>`` where the normalized arch
# is produced by ``normalize_arch`` (e.g. Windows ``AMD64`` -> ``x64``,
# ``ARM64`` -> ``arm64``). This keeps the key, the ``MXC_BIN_DIR/<arch>/``
# lookup, and the npm ``bin/<arch>/`` layout consistent across platforms.
PINNED_HASHES: dict[str, str] = {
    "darwin-arm64": "700e9e7120c78fe9ecdb8c99309ba6df0ea467ac5b581b803b73d655bbccff36",
    "darwin-x86_64": "0000000000000000000000000000000000000000000000000000000000000000",
    "win32-arm64": "e430d0e4f44f616e91db684f8d825a6dc93e06a1262b8d00bcaac7522a317aab",
    "win32-x64": "db0a3422be9e1b396cc1b2547c70ff16b27412438a31c10a45abf370cac86ae2",
    "linux-x86_64": "0000000000000000000000000000000000000000000000000000000000000000",
}


def normalize_arch(platform_name: str, machine: str) -> str:
    """Normalize a ``platform.machine()`` value to a canonical arch token.

    ``platform.machine()`` is inconsistent across platforms and runtimes
    (Windows reports ``AMD64`` / ``ARM64`` in upper case; macOS reports
    ``arm64`` / ``x86_64``; Linux reports ``x86_64`` / ``aarch64``). This maps
    those onto the per-platform token used for both the pinned-hash key and the
    ``<dir>/<arch>/<binary>`` resolution layout.

    Windows uses the npm package's ``bin`` subdirectory names (``x64`` /
    ``arm64``); macOS and Linux keep the ``x86_64`` / ``arm64`` spelling already
    used by ``PINNED_HASHES``.
    """
    m = machine.lower()
    if platform_name == "win32":
        if m in ("arm64", "aarch64"):
            return "arm64"
        # AMD64, x86_64, x64 all collapse to the npm "x64" subdir name.
        return "x64"
    # darwin / linux
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    return m


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
        arch = _platform_module.machine()
    
    # Normalize the arch to the canonical per-platform token so the
    # ``<dir>/<arch>/<binary>`` lookup matches the npm ``bin/<arch>/`` layout
    # (e.g. Windows ``AMD64`` -> ``x64``, ``ARM64`` -> ``arm64``).
    arch = normalize_arch(platform, arch)
    
    binary_name = get_binary_name(platform)
    
    # 1. Check MXC_BIN_DIR
    mxc_bin_dir = os.environ.get("MXC_BIN_DIR")
    if mxc_bin_dir:
        # Try with arch subdirectory first
        bin_path = Path(mxc_bin_dir) / arch / binary_name
        if bin_path.exists():
            return str(bin_path)
        
        # Fallback: try directly in MXC_BIN_DIR (for setup script compatibility)
        bin_path = Path(mxc_bin_dir) / binary_name
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
    platform_name: str | None = None,
    arch: str | None = None,
) -> str:
    """Resolve and verify MXC binary.
    
    Combines resolve_binary() + verify_binary() with pinned hash lookup.
    
    Args:
        platform_name: Platform name (defaults to sys.platform)
        arch: Architecture (defaults to platform.machine())
    
    Returns:
        Absolute path to verified binary
    
    Raises:
        SandboxUnavailableError: Binary not found
        SandboxUntrustedBinaryError: Binary hash mismatch
    """
    if platform_name is None:
        import sys
        platform_name = sys.platform
    
    if arch is None:
        arch = _platform_module.machine()
    
    # Normalize arch so the hash key and binary lookup agree across platforms
    # (Windows ``platform.machine()`` is upper case: ``AMD64`` / ``ARM64``).
    arch = normalize_arch(platform_name, arch)
    
    # Resolve binary
    binary_path = resolve_binary(platform_name, arch)
    if binary_path is None:
        raise SandboxUnavailableError(
            f"MXC binary not found for {platform_name}-{arch}. "
            f"Set MXC_BIN_DIR or install @microsoft/mxc-sdk via npm."
        )
    
    # Get expected hash for platform-arch combo
    hash_key = f"{platform_name}-{arch}"
    expected_hash = PINNED_HASHES.get(hash_key)
    
    if expected_hash is None:
        raise SandboxUnavailableError(
            f"No pinned hash for {hash_key}. Supported: {list(PINNED_HASHES.keys())}"
        )
    
    # Verify hash
    verify_binary(binary_path, expected_hash)
    
    return binary_path
