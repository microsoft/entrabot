"""
Sandbox policy building, clamping, and discovery helpers.

Security model:
- Positive-allowlist-only (no reliance on deniedPaths)
- Operator-set ceiling, LLM can only narrow (Learning #54)
- Backend-aware fail-closed (refuse if primitive unenforceable)
- keychain_access hardcoded False, not overridable
"""

import json
import os
import sys
import tempfile

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
    }
    # NOTE: keychain access is intentionally NOT emitted as a top-level field.
    # No MXC schema version (0.6.0-alpha / 0.7.0-alpha) defines a top-level
    # ``keychainAccess`` key, and the real ``wxc-exec.exe`` parser rejects
    # unknown top-level fields (``Unknown top-level field(s) in config:
    # keychainAccess``). On macOS keychain access is governed by
    # ``experimental.seatbelt.keychainAccess`` instead; here it stays denied by
    # default-deny. ``policy.keychain_access`` is hardcoded False and never
    # widened (see clamp_to_ceiling), so omitting the field is the correct,
    # cross-platform-safe behaviour — not a relaxation.
    
    # Add allowedHosts if specified (best-effort on macOS)
    if policy.allowed_hosts:
        config["network"]["allowedHosts"] = policy.allowed_hosts
    
    # Add env if specified
    if policy.env:
        config["process"]["env"] = [f"{k}={v}" for k, v in policy.env.items()]
    
    return json.dumps(config, indent=2)


def _normalize_for_match(path: str) -> str:
    """Canonicalize a path for ceiling comparison (no existence requirement).

    Expands ``~``, resolves symlinks where components exist, and normalizes
    ``.``/``..`` and trailing slashes via ``os.path.realpath``. Unlike
    ``canonicalize_paths``, this never raises on nonexistent paths — it is used
    only for set-membership comparison, not filesystem validation.
    """
    return os.path.realpath(os.path.expanduser(path))


def _path_within_ceiling(requested: str, ceiling_paths: list[str]) -> bool:
    """Return True if ``requested`` is equal to, or a descendant of, a ceiling dir.

    Comparison is on canonicalized real paths so that symlinks are resolved
    before the containment check (preventing symlink-escape widening), and
    differing spellings (``~``, trailing slashes, ``..``) of the same location
    match correctly.
    """
    req = _normalize_for_match(requested)
    for ceiling in ceiling_paths:
        ceil = _normalize_for_match(ceiling)
        if req == ceil:
            return True
        prefix = ceil.rstrip(os.sep) + os.sep
        if req.startswith(prefix):
            return True
    return False


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
    
    # Clamp paths to ceiling.
    #
    # Matching is done on *canonicalized* paths (expanduser + realpath, which
    # resolves symlinks and normalizes ``.``/``..`` and trailing slashes), and a
    # request is admitted if it is equal to, or a descendant of, a ceiling entry.
    #
    # Order is load-bearing for security: canonicalization happens BEFORE the
    # containment check, so a symlink located inside a granted directory cannot
    # smuggle access to a target outside the ceiling (the realpath resolves the
    # symlink to its true target, which then fails containment). Doing a naive
    # string-prefix check on un-resolved paths would reintroduce that escape.
    #
    # The original request strings are returned (not the canonical forms) so the
    # downstream ``canonicalize_paths`` step can validate existence and resolve
    # them for the backend exactly as before.
    clamped_readonly = [
        p
        for p in llm_policy.readonly_paths
        if _path_within_ceiling(p, ceiling.readonly_paths)
    ]
    clamped_readwrite = [
        p
        for p in llm_policy.readwrite_paths
        if _path_within_ceiling(p, ceiling.readwrite_paths)
    ]
    
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
    
    - Expand ``~`` to the user's home directory
    - Resolve symlinks to real paths
    - Convert to absolute paths
    - Reject nonexistent paths
    
    Raises:
        SandboxPolicyError: Path does not exist
    """
    canonicalized = []
    for path in paths:
        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            raise SandboxPolicyError(f"Path does not exist: {path}")

        # Resolve symlinks and make absolute using the host path implementation.
        canonicalized.append(os.path.realpath(expanded))
    
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
        "home_dir": os.path.expanduser("~"),
    }
