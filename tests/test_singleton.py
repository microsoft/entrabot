"""Tests for entraclaw.singleton — process-singleton flock for entraclaw-mcp.

Background: GitHub issue #62. Two simultaneous MCP clients each spawn their
own entraclaw-mcp subprocess (correct per stdio spec) and race on macOS
Keychain, the local interaction log, and the Azure Blob container. The
singleton lock fails the second spawn loudly with a clean stderr message
and exit code 2 instead of letting it die silently.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock-based singleton is POSIX-only; Windows path is a no-op fallback",
)


# ---------------------------------------------------------------------------
# Helpers for cross-process contention tests
# ---------------------------------------------------------------------------


def _hold_lock_until_signaled(lock_path: str, ready: mp.Event, release: mp.Event) -> None:
    """Subprocess body: acquire lock, signal ready, hold until told to release."""
    from entraclaw.singleton import acquire_singleton_lock

    handle = acquire_singleton_lock(Path(lock_path))
    ready.set()
    release.wait(timeout=10.0)
    handle.close()


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    """Per-test lock path under pytest's tmp_path so tests can't collide."""
    return tmp_path / "singleton.lock"


# ---------------------------------------------------------------------------
# acquire_singleton_lock — core API
# ---------------------------------------------------------------------------


def test_acquire_returns_handle_with_path_and_fd(lock_path: Path) -> None:
    from entraclaw.singleton import acquire_singleton_lock

    handle = acquire_singleton_lock(lock_path)
    try:
        assert handle.path == lock_path
        assert isinstance(handle._fd, int)
        assert handle._fd >= 0
    finally:
        handle.close()


def test_acquire_creates_parent_directory(tmp_path: Path) -> None:
    """Lock dir is auto-created so callers don't have to."""
    from entraclaw.singleton import acquire_singleton_lock

    nested = tmp_path / "deeply" / "nested" / "dir" / "singleton.lock"
    handle = acquire_singleton_lock(nested)
    try:
        assert nested.parent.exists()
        assert nested.exists()
    finally:
        handle.close()


def test_acquire_writes_holder_pid_file(lock_path: Path) -> None:
    """The .holder.pid sidecar reflects the holding PID for diagnostics."""
    from entraclaw.singleton import acquire_singleton_lock

    handle = acquire_singleton_lock(lock_path)
    try:
        holder_file = lock_path.with_suffix(lock_path.suffix + ".holder.pid")
        assert holder_file.exists()
        assert holder_file.read_text().strip() == str(os.getpid())
    finally:
        handle.close()


def test_close_releases_lock_and_removes_holder_pid(lock_path: Path) -> None:
    from entraclaw.singleton import acquire_singleton_lock

    handle = acquire_singleton_lock(lock_path)
    holder_file = lock_path.with_suffix(lock_path.suffix + ".holder.pid")
    assert holder_file.exists()

    handle.close()

    assert not holder_file.exists(), "holder.pid sidecar must be removed on close"

    # After release, a fresh acquire from the same process must succeed.
    fresh = acquire_singleton_lock(lock_path)
    try:
        assert holder_file.exists()
    finally:
        fresh.close()


def test_close_is_idempotent(lock_path: Path) -> None:
    """Double-close must not raise — destructors and explicit close coexist."""
    from entraclaw.singleton import acquire_singleton_lock

    handle = acquire_singleton_lock(lock_path)
    handle.close()
    handle.close()


def test_context_manager_releases_on_exit(lock_path: Path) -> None:
    from entraclaw.singleton import acquire_singleton_lock

    holder_file = lock_path.with_suffix(lock_path.suffix + ".holder.pid")
    with acquire_singleton_lock(lock_path):
        assert holder_file.exists()
    assert not holder_file.exists()


# ---------------------------------------------------------------------------
# Contention — only meaningful across processes (POSIX flock is per-process)
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_acquire_raises_when_held_by_another_process(lock_path: Path) -> None:
    """A second acquire from a different process must raise SingletonLockHeldError."""
    from entraclaw.singleton import SingletonLockHeldError, acquire_singleton_lock

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_lock_until_signaled,
        args=(str(lock_path), ready, release),
    )
    holder.start()
    try:
        assert ready.wait(timeout=10.0), "subprocess never acquired lock"

        with pytest.raises(SingletonLockHeldError) as excinfo:
            acquire_singleton_lock(lock_path)

        assert excinfo.value.holder_pid == holder.pid
    finally:
        release.set()
        holder.join(timeout=10.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)


@POSIX_ONLY
def test_held_error_holder_pid_falls_back_to_none_when_sidecar_missing(
    lock_path: Path,
) -> None:
    """If holder.pid was deleted out from under us, the error still raises with None."""
    from entraclaw.singleton import SingletonLockHeldError, acquire_singleton_lock

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_lock_until_signaled,
        args=(str(lock_path), ready, release),
    )
    holder.start()
    try:
        assert ready.wait(timeout=10.0)
        # Simulate sidecar tampering / external cleanup.
        holder_file = lock_path.with_suffix(lock_path.suffix + ".holder.pid")
        holder_file.unlink()

        with pytest.raises(SingletonLockHeldError) as excinfo:
            acquire_singleton_lock(lock_path)
        assert excinfo.value.holder_pid is None
    finally:
        release.set()
        holder.join(timeout=10.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)


@POSIX_ONLY
def test_acquire_succeeds_after_holder_dies_uncleanly(lock_path: Path) -> None:
    """Kernel auto-releases flock on process death; new acquire must succeed even
    when a stale .holder.pid sidecar still points at the dead PID.
    """
    from entraclaw.singleton import acquire_singleton_lock

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()  # never set — we kill the holder instead
    holder = ctx.Process(
        target=_hold_lock_until_signaled,
        args=(str(lock_path), ready, release),
    )
    holder.start()
    assert ready.wait(timeout=10.0)
    dead_pid = holder.pid

    holder.kill()  # SIGKILL — no chance for clean close()
    holder.join(timeout=10.0)

    # Holder.pid sidecar is left behind by the dead process.
    holder_file = lock_path.with_suffix(lock_path.suffix + ".holder.pid")
    if holder_file.exists():
        assert holder_file.read_text().strip() == str(dead_pid)

    # Brief delay for kernel to fully release the flock.
    time.sleep(0.05)

    # Fresh acquire must succeed (kernel released flock on death) and the
    # sidecar must now reflect THIS process, not the stale dead PID.
    fresh = acquire_singleton_lock(lock_path)
    try:
        assert holder_file.read_text().strip() == str(os.getpid())
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# run_or_exit_if_held — main()-facing wrapper
# ---------------------------------------------------------------------------


def test_run_or_exit_returns_handle_when_uncontended(lock_path: Path) -> None:
    from entraclaw.singleton import run_or_exit_if_held

    handle = run_or_exit_if_held(lock_path)
    try:
        assert handle is not None
        assert handle.path == lock_path
    finally:
        handle.close()


@POSIX_ONLY
def test_run_or_exit_exits_with_code_2_when_held(
    lock_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wrapper used in main(): on contention, exit(2) with stderr remediation."""
    from entraclaw.singleton import run_or_exit_if_held

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_lock_until_signaled,
        args=(str(lock_path), ready, release),
    )
    holder.start()
    try:
        assert ready.wait(timeout=10.0)

        with pytest.raises(SystemExit) as excinfo:
            run_or_exit_if_held(lock_path)

        assert excinfo.value.code == 2

        captured = capsys.readouterr()
        assert "[entraclaw]" in captured.err
        assert str(holder.pid) in captured.err, "stderr must name the holder PID"
        assert "refusing to start" in captured.err
    finally:
        release.set()
        holder.join(timeout=10.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)


def test_default_lock_path_uses_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default lock path lives under the configured data_dir as .singleton.lock."""
    from entraclaw.singleton import acquire_singleton_lock, default_lock_path

    monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

    expected = tmp_path / ".singleton.lock"
    assert default_lock_path() == expected

    # And acquire-with-no-args should land there.
    handle = acquire_singleton_lock()
    try:
        assert handle.path == expected
    finally:
        handle.close()
