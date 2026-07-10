"""Tests for sandbox/policy.py — policy building, clamping, discovery."""

import json
import os
import tempfile
from pathlib import Path

import pytest


# RED: Test build_policy generates valid MXC JSON
def test_build_policy_generates_mxc_json():
    """build_policy() converts SandboxPolicy to MXC 0.6.0-alpha JSON schema."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.policy import build_policy
    
    policy = SandboxPolicy(
        backend="process",
        command_line="python test.py",
        readonly_paths=["/src"],
        readwrite_paths=["/tmp/output"],
        timeout_ms=30000,
        network_default_policy="block",
    )
    
    mxc_json = build_policy(policy)
    config = json.loads(mxc_json)
    
    assert config["version"] == "0.6.0-alpha"
    assert config["containment"] == "process"
    assert config["process"]["commandLine"] == "python test.py"
    assert config["process"]["timeout"] == 30000
    assert "/src" in config["filesystem"]["readonlyPaths"]
    assert "/tmp/output" in config["filesystem"]["readwritePaths"]
    assert config["network"]["defaultPolicy"] == "block"


def test_build_policy_hardcodes_keychain_access_false():
    """build_policy() sets keychainAccess=false regardless of policy field."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.policy import build_policy
    
    policy = SandboxPolicy(
        backend="process",
        command_line="echo test",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=5000,
        keychain_access=True,  # Try to override (should be ignored)
    )
    
    mxc_json = build_policy(policy)
    config = json.loads(mxc_json)
    
    # MXC config must have keychainAccess=false (hardcoded, never true)
    assert config.get("keychainAccess") is False or "keychainAccess" not in config


def test_build_policy_includes_network_allowed_hosts():
    """build_policy() includes allowedHosts when specified (best-effort on macOS)."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.policy import build_policy
    
    policy = SandboxPolicy(
        backend="process",
        command_line="curl api.github.com",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=10000,
        network_default_policy="allow",
        allowed_hosts=["api.github.com", "example.com"],
    )
    
    mxc_json = build_policy(policy)
    config = json.loads(mxc_json)
    
    assert config["network"]["allowedHosts"] == ["api.github.com", "example.com"]


# RED: Test clamp_to_ceiling (Learning #54 guard)
def test_clamp_to_ceiling_accepts_narrowing():
    """clamp_to_ceiling() accepts policies that narrow the operator ceiling."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.policy import clamp_to_ceiling
    
    ceiling = SandboxPolicy(
        backend="process",
        command_line="",  # Will be set by LLM
        readonly_paths=["/src", "/usr/lib"],
        readwrite_paths=["/tmp", "/var/output"],
        timeout_ms=60000,
        network_default_policy="allow",
    )
    
    llm_policy = SandboxPolicy(
        backend="process",
        command_line="python main.py",
        readonly_paths=["/src"],  # Narrowed from ceiling
        readwrite_paths=["/tmp"],  # Narrowed from ceiling
        timeout_ms=30000,  # Narrowed from ceiling
        network_default_policy="block",  # Narrowed from ceiling
    )
    
    clamped = clamp_to_ceiling(llm_policy, ceiling)
    
    # Should accept narrowing
    assert clamped.readonly_paths == ["/src"]
    assert clamped.readwrite_paths == ["/tmp"]
    assert clamped.timeout_ms == 30000
    assert clamped.network_default_policy == "block"


def test_clamp_to_ceiling_clamps_widening():
    """clamp_to_ceiling() clamps policies that try to widen beyond ceiling."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.policy import clamp_to_ceiling
    
    ceiling = SandboxPolicy(
        backend="process",
        command_line="",
        readonly_paths=["/src"],
        readwrite_paths=["/tmp"],
        timeout_ms=30000,
        network_default_policy="block",
    )
    
    llm_policy = SandboxPolicy(
        backend="process",
        command_line="python main.py",
        readonly_paths=["/src", "/etc"],  # Tries to widen
        readwrite_paths=["/tmp", "/home"],  # Tries to widen
        timeout_ms=120000,  # Tries to widen
        network_default_policy="allow",  # Tries to widen
    )
    
    clamped = clamp_to_ceiling(llm_policy, ceiling)
    
    # Should clamp to ceiling, never widen
    assert set(clamped.readonly_paths) == {"/src"}  # /etc removed
    assert set(clamped.readwrite_paths) == {"/tmp"}  # /home removed
    assert clamped.timeout_ms == 30000  # Clamped
    assert clamped.network_default_policy == "block"  # Clamped


def test_clamp_to_ceiling_prevents_keychain_access_override():
    """clamp_to_ceiling() enforces keychain_access=False, LLM cannot flip it."""
    from entrabot.sandbox.base import SandboxPolicy
    from entrabot.sandbox.policy import clamp_to_ceiling
    
    ceiling = SandboxPolicy(
        backend="process",
        command_line="",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=30000,
        keychain_access=False,  # Hardcoded
    )
    
    llm_policy = SandboxPolicy(
        backend="process",
        command_line="python main.py",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=30000,
        keychain_access=True,  # LLM tries to enable
    )
    
    clamped = clamp_to_ceiling(llm_policy, ceiling)
    
    # Must remain False
    assert clamped.keychain_access is False


def test_clamp_to_ceiling_backend_aware_fail_closed():
    """clamp_to_ceiling() fails closed when policy needs unenforceable primitive."""
    from entrabot.sandbox.base import SandboxBackendUnsupportedError, SandboxPolicy
    from entrabot.sandbox.policy import clamp_to_ceiling
    
    ceiling = SandboxPolicy(
        backend="seatbelt",  # macOS backend
        command_line="",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=30000,
        network_default_policy="block",
        allowed_hosts=[],  # Empty = no host filtering
    )
    
    llm_policy = SandboxPolicy(
        backend="seatbelt",
        command_line="curl api.github.com",
        readonly_paths=[],
        readwrite_paths=[],
        timeout_ms=30000,
        network_default_policy="allow",
        allowed_hosts=["api.github.com"],  # Needs DNS filtering (unsupported)
    )
    
    # Should fail closed: seatbelt can't enforce allowedHosts
    error_pattern = "allowedHosts.*not.*supported.*seatbelt"
    with pytest.raises(SandboxBackendUnsupportedError, match=error_pattern):
        clamp_to_ceiling(llm_policy, ceiling, backend_capabilities={
            "backend": "seatbelt",
            "network_host_filtering": False,
        })


# RED: clamp path matching must canonicalize then check containment.
# These guard against the exact-string-match brittleness (Problem 1a/1b)
# while preserving the symlink-escape fail-closed property.
def _clamp_policy(readonly=None, readwrite=None):
    """Build a minimal SandboxPolicy for clamp matching tests."""
    from entrabot.sandbox.base import SandboxPolicy

    return SandboxPolicy(
        backend="process",
        command_line="cmd",
        readonly_paths=readonly or [],
        readwrite_paths=readwrite or [],
        timeout_ms=30000,
        network_default_policy="block",
    )


def test_clamp_admits_subpath_of_ceiling_dir():
    """A request to narrow into a subdirectory of a granted dir is admitted (Problem 1b)."""
    from entrabot.sandbox.policy import clamp_to_ceiling

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        sub = os.path.join(d, "out")
        os.mkdir(sub)

        ceiling = _clamp_policy(readwrite=[d])
        llm = _clamp_policy(readwrite=[sub])

        clamped = clamp_to_ceiling(llm, ceiling)

        assert clamped.readwrite_paths == [sub]


def test_clamp_admits_trailing_slash_variant():
    """A trailing-slash spelling of a granted dir is admitted (Problem 1a)."""
    from entrabot.sandbox.policy import clamp_to_ceiling

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)

        ceiling = _clamp_policy(readwrite=[d])
        llm = _clamp_policy(readwrite=[d + "/"])

        clamped = clamp_to_ceiling(llm, ceiling)

        assert clamped.readwrite_paths == [d + "/"]


def test_clamp_expands_tilde_against_absolute_ceiling():
    """A ``~`` request matches an absolute-home ceiling entry (Problem 1a)."""
    from entrabot.sandbox.policy import clamp_to_ceiling

    home_abs = os.path.realpath(os.path.expanduser("~"))

    ceiling = _clamp_policy(readonly=[home_abs])
    llm = _clamp_policy(readonly=["~"])

    clamped = clamp_to_ceiling(llm, ceiling)

    assert clamped.readonly_paths == ["~"]


def test_clamp_rejects_path_outside_ceiling_dir():
    """A sibling that merely shares a string prefix is rejected (no prefix-collision widening)."""
    from entrabot.sandbox.policy import clamp_to_ceiling

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        granted = os.path.join(d, "tmp")
        sibling = os.path.join(d, "tmpsecret")  # shares "tmp" prefix, NOT a child
        os.mkdir(granted)
        os.mkdir(sibling)

        ceiling = _clamp_policy(readwrite=[granted])
        llm = _clamp_policy(readwrite=[sibling])

        clamped = clamp_to_ceiling(llm, ceiling)

        assert clamped.readwrite_paths == []


@pytest.mark.skipif(
    os.name != "nt", reason="case-insensitive containment is a Windows concern"
)
def test_clamp_admits_case_insensitive_subpath_on_windows():
    """On Windows (case-insensitive FS) a differently-cased request is admitted.

    Windows paths are case-insensitive, so a request spelled with different case
    than the granted ceiling entry must still be contained, not silently dropped.
    """
    from entrabot.sandbox.policy import clamp_to_ceiling

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        sub = os.path.join(d, "Output")
        os.mkdir(sub)

        ceiling = _clamp_policy(readwrite=[sub])
        # Same directory, lower-cased spelling.
        llm = _clamp_policy(readwrite=[sub.lower()])

        clamped = clamp_to_ceiling(llm, ceiling)

        assert clamped.readwrite_paths == [sub.lower()]


def test_clamp_blocks_symlink_escape_from_ceiling_dir():
    """A symlink inside a granted dir that points outside it is rejected (security).

    This is the load-bearing property: containment must be checked AFTER
    canonicalization, so a symlink under a granted directory cannot smuggle
    write access to a target outside the ceiling.
    """
    from entrabot.sandbox.policy import clamp_to_ceiling

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        granted = os.path.join(d, "granted")
        secret = os.path.join(d, "secret")
        os.mkdir(granted)
        os.mkdir(secret)
        evil = os.path.join(granted, "evil")
        try:
            os.symlink(secret, evil)  # granted/evil -> ../secret (escapes ceiling)
        except OSError as e:
            # Creating symlinks on Windows requires SeCreateSymbolicLinkPrivilege
            # (admin or Developer Mode). The canonicalize-then-contain property is
            # validated on POSIX / privileged hosts; skip where unprivileged.
            pytest.skip(f"symlink creation not permitted on this host: {e}")

        ceiling = _clamp_policy(readwrite=[granted])
        llm = _clamp_policy(readwrite=[evil])

        clamped = clamp_to_ceiling(llm, ceiling)

        assert clamped.readwrite_paths == []


# RED: Test path canonicalization
def test_canonicalize_paths_resolves_symlinks():
    """Paths are canonicalized to prevent symlink escapes."""

    from entrabot.sandbox.policy import canonicalize_paths
    
    with tempfile.TemporaryDirectory() as tmpdir:
        real_path = Path(tmpdir) / "real"
        real_path.mkdir()
        
        symlink_path = Path(tmpdir) / "link"
        try:
            symlink_path.symlink_to(real_path)
        except OSError as e:
            # Windows symlink creation needs elevated privilege / Developer Mode.
            pytest.skip(f"symlink creation not permitted on this host: {e}")
        
        # Pass symlink, should resolve to real path
        canonicalized = canonicalize_paths([str(symlink_path)])
        
        # Should resolve to real absolute path
        assert str(real_path.resolve()) in canonicalized
        assert "link" not in canonicalized[0]  # Symlink name removed


def test_canonicalize_paths_rejects_nonexistent():
    """canonicalize_paths() rejects nonexistent paths."""
    from entrabot.sandbox.base import SandboxPolicyError
    from entrabot.sandbox.policy import canonicalize_paths

    with pytest.raises(SandboxPolicyError, match="does not exist"):
        canonicalize_paths(["/nonexistent/path/12345"])


def test_canonicalize_paths_expands_tilde():
    """canonicalize_paths() expands ``~`` to the user's home directory.

    The hardened clamp admits ``~``-spelled requests, so the downstream
    canonicalizer must expand them rather than treating ``~/x`` as a literal
    (nonexistent) relative path.
    """
    from entrabot.sandbox.policy import canonicalize_paths

    home = os.path.realpath(os.path.expanduser("~"))
    result = canonicalize_paths(["~"])

    assert result == [home]


# RED: Test discovery helpers
def test_get_python_discovery_paths():
    """Discovery helper finds Python interpreter and common lib paths."""

    from entrabot.sandbox.policy import get_python_discovery_paths
    
    paths = get_python_discovery_paths()
    
    assert "python_executable" in paths
    assert Path(paths["python_executable"]).exists()
    # Should include stdlib (varies by platform)
    assert "stdlib_paths" in paths
    assert isinstance(paths["stdlib_paths"], list)


def test_get_temp_discovery_paths():
    """Discovery helper finds system temp directory."""
    from entrabot.sandbox.policy import get_temp_discovery_paths
    
    paths = get_temp_discovery_paths()
    
    assert "temp_dir" in paths
    assert Path(paths["temp_dir"]).exists()
    # Should be writable
    test_file = Path(paths["temp_dir"]) / "mxc_test"
    test_file.write_text("test")
    test_file.unlink()


def test_get_user_profile_discovery_paths():
    """Discovery helper finds user home directory."""
    from entrabot.sandbox.policy import get_user_profile_discovery_paths
    
    paths = get_user_profile_discovery_paths()
    
    assert "home_dir" in paths
    assert Path(paths["home_dir"]).exists()
