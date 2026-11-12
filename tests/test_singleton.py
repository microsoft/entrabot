"""Tests for entrabot.singleton — the process-singleton lock for entrabot-mcp.

Background: Learning #56 in engineering-history/research/hard-won-learnings.md. Two
simultaneous MCP clients each spawn their own entrabot-mcp subprocess (correct per stdio
spec) and race on the cert key, the local interaction log, and the Azure Blob container.
The singleton lock fails the second spawn loudly with a clean stderr message and exit
code 2 instead of letting it die silently.

The lock is ``fcntl.flock`` on POSIX and ``msvcrt.locking`` on Windows. The cross-process
tests below run against whichever is real on the platform. The Windows branch is also run
everywhere against a stand-in ``msvcrt``, so its logic is covered on macOS and Linux CI.
"""

from __future__ import annotations

import errno
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="patches fcntl.flock")


# ---------------------------------------------------------------------------
# Helpers for cross-process contention tests
# ---------------------------------------------------------------------------


def _hold_lock_until_signaled(lock_path: str, ready: mp.Event, release: mp.Event) -> None:
    """Subprocess body: acquire lock, signal ready, hold until told to release."""
    from entrabot.singleton import acquire_singleton_lock

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
    from entrabot.singleton import acquire_singleton_lock

    handle = acquire_singleton_lock(lock_path)
    try:
        assert handle.path == lock_path
        assert isinstance(handle._fd, int)
        assert handle._fd >= 0
    finally:
        handle.close()


def test_acquire_creates_parent_directory(tmp_path: Path) -> None:
    """Lock dir is auto-created so callers don't have to."""
    from entrabot.singleton import acquire_singleton_lock

    nested = tmp_path / "deeply" / "nested" / "dir" / "singleton.lock"
    handle = acquire_singleton_lock(nested)
    try:
        assert nested.parent.exists()
        assert nested.exists()
    finally:
        handle.close()


def test_acquire_writes_holder_pid_file(lock_path: Path) -> None:
    """The .holder.pid sidecar reflects the holding PID for diagnostics."""
    from entrabot.singleton import acquire_singleton_lock

    handle = acquire_singleton_lock(lock_path)
    try:
        holder_file = lock_path.with_suffix(lock_path.suffix + ".holder.pid")
        assert holder_file.exists()
        assert holder_file.read_text().strip() == str(os.getpid())
    finally:
        handle.close()


def test_close_releases_lock_and_removes_holder_pid(lock_path: Path) -> None:
    from entrabot.singleton import acquire_singleton_lock

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
    from entrabot.singleton import acquire_singleton_lock

    handle = acquire_singleton_lock(lock_path)
    handle.close()
    handle.close()


def test_context_manager_releases_on_exit(lock_path: Path) -> None:
    from entrabot.singleton import acquire_singleton_lock

    holder_file = lock_path.with_suffix(lock_path.suffix + ".holder.pid")
    with acquire_singleton_lock(lock_path):
        assert holder_file.exists()
    assert not holder_file.exists()


# ---------------------------------------------------------------------------
# Contention — only meaningful across processes (POSIX flock is per-process)
# ---------------------------------------------------------------------------


def test_acquire_raises_when_held_by_another_process(lock_path: Path) -> None:
    """A second acquire from a different process must raise SingletonLockHeldError."""
    from entrabot.singleton import SingletonLockHeldError, acquire_singleton_lock

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


def test_held_error_holder_pid_falls_back_to_none_when_sidecar_missing(
    lock_path: Path,
) -> None:
    """If holder.pid was deleted out from under us, the error still raises with None."""
    from entrabot.singleton import SingletonLockHeldError, acquire_singleton_lock

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


def test_acquire_succeeds_after_holder_dies_uncleanly(lock_path: Path) -> None:
    """The OS releases the lock on process death; new acquire must succeed even
    when a stale .holder.pid sidecar still points at the dead PID.
    """
    from entrabot.singleton import SingletonLockHeldError, acquire_singleton_lock

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

    # Fresh acquire must succeed (the OS released the lock on death) and the
    # sidecar must now reflect THIS process, not the stale dead PID. The release
    # isn't instant: Windows documents that it "depends upon available system
    # resources", so allow it a few seconds rather than one fixed sleep.
    deadline = time.monotonic() + 5.0
    while True:
        try:
            fresh = acquire_singleton_lock(lock_path)
            break
        except SingletonLockHeldError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    try:
        assert holder_file.read_text().strip() == str(os.getpid())
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# run_or_exit_if_held — main()-facing wrapper
# ---------------------------------------------------------------------------


def test_run_or_exit_returns_handle_when_uncontended(lock_path: Path) -> None:
    from entrabot.singleton import run_or_exit_if_held

    handle = run_or_exit_if_held(lock_path)
    try:
        assert handle is not None
        assert handle.path == lock_path
    finally:
        handle.close()


def test_run_or_exit_exits_with_code_2_when_held(
    lock_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wrapper used in main(): on contention, exit(2) with stderr remediation."""
    from entrabot.singleton import run_or_exit_if_held

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
        assert "[entrabot]" in captured.err
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
    from entrabot.singleton import acquire_singleton_lock, default_lock_path

    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

    expected = tmp_path / ".singleton.lock"
    assert default_lock_path() == expected

    # And acquire-with-no-args should land there.
    handle = acquire_singleton_lock()
    try:
        assert handle.path == expected
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Error paths — the descriptor is closed whenever no handle is returned (#59)
# ---------------------------------------------------------------------------


class _DescriptorLog:
    """Records the descriptors ``acquire_singleton_lock`` opens and the ones that get closed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, lock_path: Path) -> None:
        self.opened: list[int] = []
        self.closed: list[int] = []
        real_open, real_close = os.open, os.close

        def recording_open(path: object, *args: object, **kwargs: object) -> int:
            fd = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
            if Path(os.fspath(path)) == lock_path:  # type: ignore[arg-type]
                self.opened.append(fd)
            return fd

        def recording_close(fd: int) -> None:
            real_close(fd)
            self.closed.append(fd)

        monkeypatch.setattr(os, "open", recording_open)
        monkeypatch.setattr(os, "close", recording_close)

    def assert_nothing_left_open(self) -> None:
        assert self.opened, "the lock file was never opened"
        assert set(self.opened) <= set(self.closed), "a lock file descriptor was leaked"


@POSIX_ONLY
def test_posix_unexpected_flock_error_propagates_and_closes_the_descriptor(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #59: an ``OSError`` other than "already locked" used to leak the descriptor."""
    import fcntl

    from entrabot.singleton import SingletonLockHeldError, acquire_singleton_lock

    def no_locks_available(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks_available)
    fds = _DescriptorLog(monkeypatch, lock_path)

    with pytest.raises(OSError) as excinfo:
        acquire_singleton_lock(lock_path)

    assert not isinstance(excinfo.value, SingletonLockHeldError)
    assert excinfo.value.errno == errno.ENOLCK
    fds.assert_nothing_left_open()


def test_holder_pid_write_failure_releases_the_lock(
    lock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock taken but never handed back would shut out every later start in this process."""
    from entrabot import singleton

    real_holder_pid_path = singleton._holder_pid_path
    monkeypatch.setattr(
        singleton, "_holder_pid_path", lambda path: path.parent / "no-such-dir" / "holder.pid"
    )
    fds = _DescriptorLog(monkeypatch, lock_path)

    with pytest.raises(OSError):
        singleton.acquire_singleton_lock(lock_path)

    fds.assert_nothing_left_open()
    monkeypatch.setattr(singleton, "_holder_pid_path", real_holder_pid_path)
    singleton.acquire_singleton_lock(lock_path).close()  # not still held by the failed attempt


# ---------------------------------------------------------------------------
# Windows — msvcrt.locking, run here against a stand-in for the module
# ---------------------------------------------------------------------------


class _FakeMsvcrt:
    """Stand-in for ``msvcrt`` with the one behaviour the lock relies on.

    ``locking`` takes or releases a byte-range lock at the descriptor's current position.
    Windows enforces it across descriptors and processes: a second lock on a byte that is
    already locked fails with ``EACCES``. This keys locks on the file's identity, so two
    descriptors on one file contend here exactly as two processes do there.
    """

    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self) -> None:
        self.held: dict[tuple[int, int], int] = {}
        self.calls: list[tuple[int, int, int]] = []
        self.fail_with: int | None = None

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((fd, mode, nbytes))
        if self.fail_with is not None:
            raise OSError(self.fail_with, os.strerror(self.fail_with))
        assert nbytes == 1
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0, "lock and unlock must name the same byte"
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        if mode == self.LK_UNLCK:
            if self.held.get(key) != fd:
                raise OSError(errno.EACCES, "Permission denied")
            del self.held[key]
            return
        assert mode == self.LK_NBLCK, "the lock must not block: LK_LOCK retries for 10 seconds"
        if key in self.held:
            raise OSError(errno.EACCES, "Permission denied")
        self.held[key] = fd


@pytest.fixture
def fake_windows(monkeypatch: pytest.MonkeyPatch) -> _FakeMsvcrt:
    """Send ``entrabot.singleton`` down its Windows branch, against a stand-in ``msvcrt``."""
    from entrabot import singleton

    fake = _FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(singleton.sys, "platform", "win32")
    return fake


def test_windows_second_acquire_is_refused_and_names_the_holder(
    lock_path: Path, fake_windows: _FakeMsvcrt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock used to be a no-op on Windows: a second server started and polled alongside."""
    from entrabot.singleton import SingletonLockHeldError, acquire_singleton_lock

    first = acquire_singleton_lock(lock_path)
    fds = _DescriptorLog(monkeypatch, lock_path)
    try:
        with pytest.raises(SingletonLockHeldError) as excinfo:
            acquire_singleton_lock(lock_path)

        assert excinfo.value.holder_pid == os.getpid()
        fds.assert_nothing_left_open()
    finally:
        first.close()


def test_windows_refused_attempt_leaves_the_holders_pid_file_alone(
    lock_path: Path, fake_windows: _FakeMsvcrt
) -> None:
    """The no-op lock wrote its own PID before anything else, clobbering the real holder's."""
    from entrabot.singleton import SingletonLockHeldError, acquire_singleton_lock

    holder_file = lock_path.with_suffix(lock_path.suffix + ".holder.pid")
    first = acquire_singleton_lock(lock_path)
    try:
        holder_file.write_text("4242\n")  # as if another process held the lock
        with pytest.raises(SingletonLockHeldError) as excinfo:
            acquire_singleton_lock(lock_path)
        assert excinfo.value.holder_pid == 4242
        assert holder_file.read_text() == "4242\n"
    finally:
        first.close()


def test_windows_close_unlocks_so_the_next_acquire_succeeds(
    lock_path: Path, fake_windows: _FakeMsvcrt
) -> None:
    from entrabot.singleton import acquire_singleton_lock

    first = acquire_singleton_lock(lock_path)
    first.close()

    assert fake_windows.held == {}
    assert (first._fd, fake_windows.LK_UNLCK, 1) in fake_windows.calls
    acquire_singleton_lock(lock_path).close()


def test_windows_run_or_exit_exits_with_code_2_when_held(
    lock_path: Path, fake_windows: _FakeMsvcrt, capsys: pytest.CaptureFixture[str]
) -> None:
    from entrabot.singleton import acquire_singleton_lock, run_or_exit_if_held

    first = acquire_singleton_lock(lock_path)
    try:
        with pytest.raises(SystemExit) as excinfo:
            run_or_exit_if_held(lock_path)
        assert excinfo.value.code == 2
        assert f"PID {os.getpid()}" in capsys.readouterr().err
    finally:
        first.close()


def test_windows_unexpected_locking_error_propagates_and_closes_the_descriptor(
    lock_path: Path, fake_windows: _FakeMsvcrt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only "already locked" means another instance. Anything else is a real error."""
    from entrabot.singleton import SingletonLockHeldError, acquire_singleton_lock

    fake_windows.fail_with = errno.EINVAL
    fds = _DescriptorLog(monkeypatch, lock_path)

    with pytest.raises(OSError) as excinfo:
        acquire_singleton_lock(lock_path)

    assert not isinstance(excinfo.value, SingletonLockHeldError)
    assert excinfo.value.errno == errno.EINVAL
    fds.assert_nothing_left_open()


def test_windows_holder_pid_write_failure_releases_the_lock(
    lock_path: Path, fake_windows: _FakeMsvcrt, monkeypatch: pytest.MonkeyPatch
) -> None:
    from entrabot import singleton

    monkeypatch.setattr(
        singleton, "_holder_pid_path", lambda path: path.parent / "no-such-dir" / "holder.pid"
    )
    fds = _DescriptorLog(monkeypatch, lock_path)

    with pytest.raises(OSError):
        singleton.acquire_singleton_lock(lock_path)

    fds.assert_nothing_left_open()
    assert fake_windows.held == {}, "the byte-range lock outlived the failed attempt"
