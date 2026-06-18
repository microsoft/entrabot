"""
Sandbox policy building, clamping, and discovery helpers.

Security model:
- Positive-allowlist-only (no reliance on deniedPaths)
- Operator-set ceiling, LLM can only narrow (Learning #54)
- Backend-aware fail-closed (refuse if primitive unenforceable)
- keychain_access hardcoded False, not overridable
"""

import json
import sys
import tempfile
from pathlib import Path

from entrabot.sandbox.base import (
    SandboxBackendUnsupportedError,
    SandboxPolicy,
    SandboxPolicyError,
)


def build_policy(policy: SandboxPolicy) -> str:
    """Convert SandboxPolicy to MXC 0.6.0-alpha JSON schema.
    
    Returns JSON string ready for stdin/file delivery to MXC binary.
    """
    config = {
        "version": "0.6.0-alpha",
        "containment": policy.backend,
        "process": {
            "commandLine": policy.command_line,
            "timeout": policy.timeout_ms,
        },
        "filesystem": {
            "readonlyPaths": policy.readonly_paths,
            "readwritePaths": policy.readwrite_paths,
        },
        "network": {
            "defaultPolicy": policy.network_default_policy,
        },
        "keychainAccess": False,  # Hardcoded, never True
    }
    
    # Add allowedHosts if specified (best-effort on macOS)
    if policy.allowed_hosts:
        config["network"]["allowedHosts"] = policy.allowed_hosts
    
    # Add env if specified
    if policy.env:
        config["process"]["env"] = [f"{k}={v}" for k, v in policy.env.items()]
    
    return json.dumps(config, indent=2)


def clamp_to_ceiling(
    llm_policy: SandboxPolicy,
    ceiling: SandboxPolicy,
    backend_capabilities: dict | None = None,
) -> SandboxPolicy:
    """Clamp LLM-requested policy to operator-defined ceiling.
    
    Learning #54: The model cannot widen its own containment.
    - LLM can NARROW (fewer paths, shorter timeout, more restrictive network)
    - LLM cannot WIDEN (more paths, longer timeout, less restrictive network)
    - keychain_access cannot be flipped to True
    
    Args:
        llm_policy: Policy requested by LLM
        ceiling: Operator-defined maximum allowances
        backend_capabilities: Optional dict with backend enforcement capabilities
    
    Returns:
        Clamped policy (never wider than ceiling)
    
    Raises:
        SandboxBackendUnsupportedError: Policy needs unenforceable primitive
    """
    # Backend-aware fail-closed checks
    if backend_capabilities:
        backend = backend_capabilities.get("backend", "unknown")
        
        # If LLM requests allowedHosts but backend can't enforce, fail closed
        network_filtering = backend_capabilities.get("network_host_filtering", False)
        if llm_policy.allowed_hosts and not network_filtering:
            raise SandboxBackendUnsupportedError(
                f"allowedHosts filtering not supported on {backend} backend"
            )
    
    # Clamp paths to ceiling (only keep paths that are in ceiling)
    ceiling_readonly = set(ceiling.readonly_paths)
    ceiling_readwrite = set(ceiling.readwrite_paths)
    
    clamped_readonly = [p for p in llm_policy.readonly_paths if p in ceiling_readonly]
    clamped_readwrite = [p for p in llm_policy.readwrite_paths if p in ceiling_readwrite]
    
    # Clamp timeout to ceiling (take minimum)
    clamped_timeout = min(llm_policy.timeout_ms, ceiling.timeout_ms)
    
    # Clamp network policy (block is most restrictive)
    # If LLM says block, keep block; if ceiling says block, force block
    if ceiling.network_default_policy == "block":
        clamped_network = "block"
    else:
        clamped_network = llm_policy.network_default_policy
    
    # keychain_access: always False, cannot be overridden
    clamped_keychain = False
    
    return SandboxPolicy(
        backend=ceiling.backend,  # Use ceiling backend
        command_line=llm_policy.command_line,  # Command is LLM-provided
        readonly_paths=clamped_readonly,
        readwrite_paths=clamped_readwrite,
        timeout_ms=clamped_timeout,
        network_default_policy=clamped_network,
        keychain_access=clamped_keychain,
        allowed_hosts=llm_policy.allowed_hosts if clamped_network == "allow" else [],
        env=llm_policy.env,  # Env vars are per-request
    )


def canonicalize_paths(paths: list[str]) -> list[str]:
    """Canonicalize paths to prevent symlink escapes.
    
    - Resolve symlinks to real paths
    - Convert to absolute paths
    - Reject nonexistent paths
    
    Raises:
        SandboxPolicyError: Path does not exist
    """
    canonicalized = []
    for path in paths:
        p = Path(path)
        if not p.exists():
            raise SandboxPolicyError(f"Path does not exist: {path}")
        
        # Resolve symlinks and make absolute
        real_path = p.resolve()
        canonicalized.append(str(real_path))
    
    return canonicalized


def get_python_discovery_paths() -> dict:
    """Discover Python interpreter and stdlib paths.
    
    Returns dict with:
        - python_executable: Path to current Python
        - stdlib_paths: List of stdlib directories
    """
    return {
        "python_executable": sys.executable,
        "stdlib_paths": [p for p in sys.path if "lib/python" in p or "lib64/python" in p],
    }


def get_temp_discovery_paths() -> dict:
    """Discover system temp directory.
    
    Returns dict with:
        - temp_dir: System temp directory path
    """
    return {
        "temp_dir": tempfile.gettempdir(),
    }


def get_user_profile_discovery_paths() -> dict:
    """Discover user home directory.
    
    Returns dict with:
        - home_dir: User home directory path
    """
    return {
        "home_dir": str(Path.home()),
    }
