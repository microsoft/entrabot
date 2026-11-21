"""A filtering D-Bus proxy for the contained server on Linux (``xdg-dbus-proxy``).

The agent's certificate key is in the Secret Service (GNOME Keyring, KWallet), which is
reached over the D-Bus session bus. That bus also carries the user's systemd, the desktop
portals and every other desktop service, so the bus itself is never given to the sandbox:
with it, a process inside can ask systemd to run a command outside (run for real on Ubuntu
24.04). The launcher runs ``xdg-dbus-proxy`` outside the sandbox instead, with ``--filter``:
the server may talk to the key store's names (:data:`TALK`) and to nothing else on the bus.
Only the proxy's socket is mounted into the sandbox.

The proxy connects to the real bus from outside, so a bus on an abstract socket works too.

What this does not narrow: the server can read and write every secret the key store holds
for this user, not only Entrabot's own. The Secret Service has no per-item rule a proxy
could apply.
"""

from __future__ import annotations

import contextlib
import os
import select
import subprocess
from pathlib import Path

EXECUTABLE = "xdg-dbus-proxy"
READY_SECONDS = 10
# GNOME Keyring and KeePassXC answer on the Secret Service name; KWallet has its own.
TALK: tuple[str, ...] = (
    "org.freedesktop.secrets",
    "org.kde.kwalletd5",
    "org.kde.kwalletd6",
)


def command(executable: str, bus_address: str, socket_path: Path, ready_fd: int) -> list[str]:
    """``--fd``: the proxy writes one byte to it when its socket is ready, and exits when the
    other end is closed, so it never outlives the launcher."""
    return [
        executable,
        f"--fd={int(ready_fd)}",
        bus_address,
        str(socket_path),
        "--filter",
        *(f"--talk={name}" for name in TALK),
    ]


class DbusProxy:
    """The running proxy. :meth:`start` returns once its socket accepts connections."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._ready_fd: int | None = None

    def start(self, executable: str, bus_address: str, socket_path: Path) -> None:
        read_fd, write_fd = os.pipe()
        try:
            self._process = subprocess.Popen(  # noqa: S603 - by full path, fixed arguments
                command(executable, bus_address, socket_path, write_fd),
                pass_fds=(write_fd,),
                stdin=subprocess.DEVNULL,
            )
        except OSError:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        self._ready_fd = read_fd
        ready, _, _ = select.select([read_fd], [], [], READY_SECONDS)
        if not ready or not os.read(read_fd, 1):
            self.stop()
            raise RuntimeError(f"{EXECUTABLE} did not start")

    def stop(self) -> None:
        if self._ready_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._ready_fd)
            self._ready_fd = None
        if self._process is not None:
            with contextlib.suppress(OSError):
                self._process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                self._process.wait(timeout=5)
            self._process = None
