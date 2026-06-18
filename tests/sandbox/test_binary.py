"""Tests for sandbox/binary.py — binary resolution and verification."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# RED: Test resolve_binary finds prebuilt binary
def test_resolve_binary_finds_prebuilt():
    """resolve_binary() finds prebuilt MXC binary in MXC_BIN_DIR."""
    from entrabot.sandbox.binary import resolve_binary
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create fake prebuilt binary structure: MXC_BIN_DIR/arm64/mxc-exec-mac
        bin_dir = Path(tmpdir) / "bin"
        arch_dir = bin_dir / "arm64"
        arch_dir.mkdir(parents=True)
        
        fake_binary = arch_dir / "mxc-exec-mac"
        fake_binary.write_text("#!/bin/sh\necho test")
        fake_binary.chmod(0o755)
        
        with patch.dict(os.environ, {"MXC_BIN_DIR": str(bin_dir)}):
            binary_path = resolve_binary(platform="darwin", arch="arm64")
            
            assert binary_path == str(fake_binary)


def test_resolve_binary_finds_npm_global():
    """resolve_binary() falls back to npm global bin if MXC_BIN_DIR unset."""
    from entrabot.sandbox.binary import resolve_binary
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Simulate npm global bin structure
        npm_bin = Path(tmpdir) / "node_modules" / "@microsoft" / "mxc-sdk" / "bin"
        npm_bin.mkdir(parents=True)
        
        fake_binary = npm_bin / "mxc-exec-mac"
        fake_binary.write_text("#!/bin/sh\necho test")
        fake_binary.chmod(0o755)
        
        # Mock npm bin lookup
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = str(npm_bin)
            
            with patch.dict(os.environ, {}, clear=True):
                binary_path = resolve_binary(platform="darwin", arch="arm64")
                
                assert "mxc-exec-mac" in binary_path


def test_resolve_binary_returns_none_when_not_found():
    """resolve_binary() returns None when no binary found (not exception)."""
    from entrabot.sandbox.binary import resolve_binary
    
    with patch.dict(os.environ, {}, clear=True), patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        
        binary_path = resolve_binary(platform="darwin", arch="arm64")
        
        assert binary_path is None


# RED: Test SHA256 verification
def test_verify_binary_accepts_good_hash():
    """verify_binary() accepts binary matching expected SHA256."""
    from entrabot.sandbox.binary import verify_binary
    
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
        test_content = b"test binary content"
        f.write(test_content)
        f.flush()
        
        # Compute actual SHA256 of test content
        import hashlib
        expected_hash = hashlib.sha256(test_content).hexdigest()
        
        try:
            # Should not raise
            verify_binary(f.name, expected_hash)
        finally:
            os.unlink(f.name)


def test_verify_binary_rejects_bad_hash():
    """verify_binary() raises SandboxUntrustedBinaryError on hash mismatch."""
    from entrabot.sandbox.base import SandboxUntrustedBinaryError
    from entrabot.sandbox.binary import verify_binary
    
    with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
        f.write(b"test binary content")
        f.flush()
        
        try:
            with pytest.raises(SandboxUntrustedBinaryError, match="SHA256 mismatch"):
                verify_binary(f.name, "wrong_hash_1234567890abcdef")
        finally:
            os.unlink(f.name)


def test_verify_binary_rejects_nonexistent():
    """verify_binary() raises SandboxUntrustedBinaryError for nonexistent file."""
    from entrabot.sandbox.base import SandboxUntrustedBinaryError
    from entrabot.sandbox.binary import verify_binary
    
    with pytest.raises(SandboxUntrustedBinaryError, match="not found"):
        verify_binary("/nonexistent/binary/path", "somehash")


# RED: Test get_binary_name per platform
def test_get_binary_name_darwin():
    """get_binary_name() returns mxc-exec-mac for macOS."""
    from entrabot.sandbox.binary import get_binary_name
    
    assert get_binary_name("darwin") == "mxc-exec-mac"


def test_get_binary_name_windows():
    """get_binary_name() returns wxc-exec.exe for Windows."""
    from entrabot.sandbox.binary import get_binary_name
    
    assert get_binary_name("win32") == "wxc-exec.exe"


def test_get_binary_name_linux():
    """get_binary_name() returns lxc-exec for Linux."""
    from entrabot.sandbox.binary import get_binary_name
    
    assert get_binary_name("linux") == "lxc-exec"


# RED: Test pinned hashes
def test_pinned_hashes_exist():
    """PINNED_HASHES dict contains expected SHA256 for known binaries."""
    from entrabot.sandbox.binary import PINNED_HASHES
    
    # Should have entries for each platform
    assert "darwin-arm64" in PINNED_HASHES or "darwin-x86_64" in PINNED_HASHES
    assert "win32-x86_64" in PINNED_HASHES or "win32-amd64" in PINNED_HASHES
    
    # Hashes should be 64-char hex strings
    for _key, hash_val in PINNED_HASHES.items():
        assert isinstance(hash_val, str)
        assert len(hash_val) == 64  # SHA256 is 64 hex chars
        assert all(c in "0123456789abcdef" for c in hash_val.lower())


# RED: Test resolve_and_verify combines both steps
def test_resolve_and_verify_happy_path():
    """resolve_and_verify() finds binary and verifies hash."""
    from entrabot.sandbox.binary import resolve_and_verify
    
    with tempfile.TemporaryDirectory() as tmpdir:
        bin_dir = Path(tmpdir) / "bin"
        arch_dir = bin_dir / "arm64"
        arch_dir.mkdir(parents=True)
        
        fake_binary = arch_dir / "mxc-exec-mac"
        test_content = b"fake mxc binary"
        fake_binary.write_bytes(test_content)
        fake_binary.chmod(0o755)
        
        import hashlib
        expected_hash = hashlib.sha256(test_content).hexdigest()
        
        with patch.dict(os.environ, {"MXC_BIN_DIR": str(bin_dir)}):
            # Mock PINNED_HASHES to accept our test hash
            from entrabot.sandbox import binary as binary_module
            original_hashes = binary_module.PINNED_HASHES.copy()
            binary_module.PINNED_HASHES["darwin-arm64"] = expected_hash
            
            try:
                binary_path = resolve_and_verify(platform_name="darwin", arch="arm64")
                assert binary_path == str(fake_binary)
            finally:
                binary_module.PINNED_HASHES = original_hashes


def test_resolve_and_verify_raises_on_hash_mismatch():
    """resolve_and_verify() raises SandboxUntrustedBinaryError on bad hash."""
    from entrabot.sandbox.base import SandboxUntrustedBinaryError
    from entrabot.sandbox.binary import resolve_and_verify
    
    with tempfile.TemporaryDirectory() as tmpdir:
        bin_dir = Path(tmpdir) / "bin"
        arch_dir = bin_dir / "arm64"
        arch_dir.mkdir(parents=True)
        
        fake_binary = arch_dir / "mxc-exec-mac"
        fake_binary.write_bytes(b"malicious binary content")
        fake_binary.chmod(0o755)
        
        with (
            patch.dict(os.environ, {"MXC_BIN_DIR": str(bin_dir)}),
            pytest.raises(SandboxUntrustedBinaryError),
        ):
            resolve_and_verify(platform_name="darwin", arch="arm64")


def test_resolve_and_verify_raises_unavailable_when_not_found():
    """resolve_and_verify() raises SandboxUnavailableError when binary not found."""
    from entrabot.sandbox.base import SandboxUnavailableError
    from entrabot.sandbox.binary import resolve_and_verify
    
    with patch.dict(os.environ, {}, clear=True), patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        
        with pytest.raises(SandboxUnavailableError, match="not found"):
            resolve_and_verify(platform_name="darwin", arch="arm64")
