"""Process-singleton lock for entrabot-mcp.

Background
----------
Every MCP stdio client that attaches to entrabot spawns a fresh subprocess
(one server per client by spec). Two simultaneous clients on the same
workstation produce two competing entrabot-mcp processes that race on:

- the macOS Keychain item holding the Agent Identity cert key
  (Keychain serializes access — one reader can block the other long
  enough that the host's MCP ``initialize`` handshake times out)
- the Teams and email polls: both processes push, and may answer, every message
- the local interaction log at ``~/.entrabot/data/interaction_log.jsonl``
- the ``watched_chats`` list (a file in local mode, a shared blob in cloud mode)
- the Azure Blob container (interactions/, daily_summaries/) — ETag races

This module's job is to fail the second spawn loudly with a clean stderr
message and exit code 2 instead of letting it die silently between module
import and ``main()``. Learning #56 in
``engineering-history/research/hard-won-learnings.md`` has the full diagnosis.

Usage
-----
::

    from entrabot.singleton import run_or_exit_if_held

    def main() -> None:
        _lock_handle = run_or_exit_if_held()  # held for process lifetime
        ...                                    # rest of main()

The lock is ``fcntl.flock`` on POSIX and a one-byte ``msvcrt.locking`` range
lock on Windows. The operating system releases either one when the holding
process exits (clean or crash), so a dead lock-holder never strands the next
spawn — the only thing we need to clean up ourselves is the ``.holder.pid``
sidecar (a diagnostic, not load-bearing).

Windows had no real lock at first: this module returned a handle that excluded
nothing, a gap no issue tracked. Setup registers the server with two MCP hosts
there, so two servers ran side by side whenever both hosts were open.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from entrabot.config import get_config

_DEFAULT_LOCK_NAME = ".singleton.lock"


class SingletonLockHeldError(RuntimeError):
    """Raised when another entrabot-mcp process already owns the lock.

    ``holder_pid`` is the PID read from the ``.holder.pid`` sidecar at the
    moment of contention, or ``None`` when the sidecar was missing or
    unreadable. It's diagnostic only — never used for authorization.
    """

    def __init__(self, holder_pid: int | None) -> None:
        self.holder_pid = holder_pid
        if holder_pid is not None:
            super().__init__(
                f"another entrabot-mcp instance is already running (PID {holder_pid})"
            )
        else:
            super().__init__("another entrabot-mcp instance is already running")


@dataclass
class SingletonLock:
    """Handle to an acquired singleton lock.

    Released on ``close()`` or when used as a context manager. The underlying
    file descriptor is held for the process lifetime; on process exit the
    operating system releases the lock automatically.
    """

    path: Path
    _fd: int
    _holder_pid_file: Path
    _closed: bool = False

    def close(self) -> None:
        """Release the lock and remove the holder PID sidecar.

        Idempotent — safe to call multiple times.
        """
        if self._closed:
            return
        self._closed = True

        _release(self._fd)

        with contextlib.suppress(OSError):
            self._holder_pid_file.unlink(missing_ok=True)

    def __enter__(self) -> SingletonLock:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _holder_pid_path(lock_path: Path) -> Path:
    """Return the ``.holder.pid`` sidecar path next to ``lock_path``."""
    return lock_path.with_suffix(lock_path.suffix + ".holder.pid")


def _read_holder_pid(lock_path: Path) -> int | None:
    """Best-effort read of the holder PID sidecar; returns None on any failure."""
    holder_file = _holder_pid_path(lock_path)
    try:
        text = holder_file.read_text(encoding="ascii", errors="replace").strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def default_lock_path() -> Path:
    """Default lock location — ``<data_dir>/.singleton.lock``.

    Reads ``ENTRABOT_DATA_DIR`` from the environment via
    :func:`entrabot.config.get_config`, so tests can monkeypatch the env.
    """
    return get_config().data_dir / _DEFAULT_LOCK_NAME


def _lock_exclusively(fd: int) -> bool:
    """Take the exclusive lock on *fd* without waiting. False means another process holds it.

    Any other failure raises: only "already locked" is evidence of a second instance.
    """
    if sys.platform == "win32":
        import msvcrt

        # A byte-range lock on the first byte, which Windows enforces between processes. The
        # file is empty and stays empty: a range past the end of a file can be locked.
        # LK_NBLCK fails at once where LK_LOCK would retry for ten seconds.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            # EACCES is "locked by another process"; EDEADLK is what the blocking modes
            # raise for the same thing once their retries run out.
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release(fd: int) -> None:
    """Unlock and close *fd*. Never raises: closing the descriptor drops the lock anyway."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)  # unlock the byte that was locked
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except (OSError, ImportError):
        pass

    with contextlib.suppress(OSError):
        os.close(fd)


def acquire_singleton_lock(path: Path | None = None) -> SingletonLock:
    """Acquire an exclusive lock on ``path``; raise if held by a live owner.

    Parameters
    ----------
    path:
        Lock file location. Defaults to ``default_lock_path()`` (under the
        configured ``data_dir``). Parent directories are created if needed.

    Returns
    -------
    SingletonLock
        Handle owning the file descriptor and ``.holder.pid`` sidecar.
        Caller MUST keep this alive for the process lifetime, or call
        ``close()`` when releasing voluntarily.

    Raises
    ------
    SingletonLockHeldError
        When another live process holds the lock. The operating system
        releases the lock automatically on process death, so getting this
        error means someone is alive RIGHT NOW.
    OSError
        When the lock file can't be opened or locked, or the ``.holder.pid``
        sidecar can't be written. Nothing is left held or open.
    """
    if path is None:
        path = default_lock_path()

    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        locked = _lock_exclusively(fd)
        if not locked:
            raise SingletonLockHeldError(_read_holder_pid(path))
        # Written only once the lock is held, so a refused attempt can't overwrite the
        # PID of the process that holds it.
        holder_file = _holder_pid_path(path)
        holder_file.write_text(f"{os.getpid()}\n", encoding="ascii")
    except BaseException:
        # Whatever went wrong, no handle is returned, so nothing could ever release this
        # descriptor: an open one leaks, and a locked one shuts out every later start.
        if locked:
            _release(fd)
        else:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise

    return SingletonLock(path=path, _fd=fd, _holder_pid_file=holder_file)


def run_or_exit_if_held(path: Path | None = None) -> SingletonLock:
    """Acquire the singleton lock or exit(2) with a remediation message.

    Designed to be the first call in ``main()``. The returned handle MUST
    be kept alive for the process lifetime; assign it to a module-level
    or function-local name and don't drop the reference.

    On contention writes a one-line ``[entrabot]`` stderr message naming
    the holder PID and suggesting remediation, then ``sys.exit(2)`` so
    MCP hosts surface the failure instead of looping on Connecting.
    """
    try:
        return acquire_singleton_lock(path)
    except SingletonLockHeldError as exc:
        pid_str = str(exc.holder_pid) if exc.holder_pid is not None else "unknown"
        sys.stderr.write(
            f"[entrabot] another instance is already running (PID {pid_str}); "
            "refusing to start. Stop that process and restart the host to take "
            "over.\n"
        )
        sys.stderr.flush()
        sys.exit(2)
