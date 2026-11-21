"""The Linux bubblewrap command for the contained server.

- **Only named folders exist inside.** The host's root is never mounted. The sandbox's root
  is bubblewrap's own empty tmpfs, with the system folders a Python process needs
  (:data:`SYSTEM_PATHS`, and the interpreter's own folder when it is elsewhere) mounted
  read-only, the repository and the virtual environment read-only, and Entrabot's state
  read-write. ``/tmp`` is a private tmpfs, and the root is remounted read-only at the end.
  So ``/run``, ``/home``, ``/mnt``, ``/media``, ``/var``, ``/root`` and ``/sys`` are not
  there at all: no other user's files, no other disk, and none of the Unix sockets under
  ``/run`` (the session and system D-Bus, systemd's private socket, the SSH and GPG agents,
  Docker, snapd). Mounting the host root read-only does not hide those sockets, because a
  read-only mount does not stop ``connect()``: run for real on Ubuntu 24.04, a process in
  such a sandbox had the user's systemd run a command outside it.
- The server gets its own network namespace (``--unshare-net``): no interface but loopback,
  and no abstract Unix socket of the host. The launcher's proxy listens on a Unix socket,
  which is mounted in, and :mod:`entrabot.sandbox.bridge` forwards a loopback port inside
  the namespace to it.
- The Secret Service (GNOME Keyring, KWallet) is reached over the D-Bus session bus. The bus
  itself is never mounted in. The launcher runs a filtering proxy outside
  (:mod:`entrabot.sandbox.dbusproxy`) that lets the server talk to the key store and to
  nothing else on the bus, and only that proxy's socket is mounted in. Without it there is
  no bus inside, and a sign-in that needs the key store fails closed.
- ``--die-with-parent``, ``--new-session`` (no terminal injection), separate user, PID,
  IPC, UTS and cgroup namespaces, and no capabilities (``--cap-drop ALL``).
- The server cannot make namespaces of its own, which would let it rearrange what it sees:
  ``--disable-userns`` where this bubblewrap has it (0.8.0 and later), and the system-call
  filter of :mod:`entrabot.sandbox.seccomp` (``--seccomp``), which also refuses the kernel
  keyring, mounts, tracing and typing into the terminal.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from entrabot.sandbox.policy import ContainmentPolicy

# In the private /tmp: bwrap can create the mount points there.
PROXY_SOCKET_INSIDE = "/tmp/entrabot-proxy.sock"
DBUS_SOCKET_INSIDE = "/tmp/entrabot-dbus.sock"
DBUS_ADDRESS_INSIDE = f"unix:path={DBUS_SOCKET_INSIDE}"

# What a Python process needs from the system: programs and libraries, and /etc for the
# loader's cache, certificates, time zone and the user database. Each is mounted only when
# it exists (on a merged-/usr system /bin, /sbin and /lib* are links into /usr).
SYSTEM_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
)


def session_bus_address(env: dict[str, str] | None) -> str | None:
    """The D-Bus session bus address the launcher itself uses, or None."""
    value = (env or {}).get("DBUS_SESSION_BUS_ADDRESS", "").strip()
    return value or None


def system_links() -> dict[str, str]:
    """Which of :data:`SYSTEM_PATHS` are symbolic links on this computer, and to what."""
    links: dict[str, str] = {}
    for path in SYSTEM_PATHS:
        if os.path.islink(path):
            links[path] = os.readlink(path)
    return links


def command(
    policy: ContainmentPolicy,
    *,
    bwrap: str,
    proxy_socket: Path,
    proxy_port: int,
    inner_argv: list[str],
    dbus_proxy_socket: Path | None = None,
    system_paths: Sequence[Path] = (),
    system_links: Mapping[str, str] | None = None,
    disable_userns: bool = False,
    seccomp_fd: int | None = None,
) -> list[str]:
    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--cap-drop",
        "ALL",
    ]
    if disable_userns:
        argv.append("--disable-userns")
    if seccomp_fd is not None:
        argv += ["--seccomp", str(int(seccomp_fd))]
    links = system_links or {}
    for path in (*SYSTEM_PATHS, *(str(extra) for extra in system_paths)):
        if path in links:
            argv += ["--symlink", links[path], path]
        else:
            argv += ["--ro-bind-try", path, path]
    argv += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        str(policy.home),
    ]
    for path in policy.read_only:
        argv += ["--ro-bind-try", str(path), str(path)]
    for path in policy.read_write:
        argv += ["--bind-try", str(path), str(path)]
    argv += ["--ro-bind", str(proxy_socket), PROXY_SOCKET_INSIDE]
    if dbus_proxy_socket is not None:
        argv += ["--ro-bind", str(dbus_proxy_socket), DBUS_SOCKET_INSIDE]
    # Everything is mounted: now the root itself (bubblewrap's tmpfs) stops being writable.
    # The mounts under it keep their own setting: the state read-write, /tmp a tmpfs.
    argv += ["--remount-ro", "/"]
    argv += [
        "--",
        *inner_argv[:1],
        "-m",
        "entrabot.sandbox.bridge",
        "--unix",
        PROXY_SOCKET_INSIDE,
        "--port",
        str(int(proxy_port)),
        "--",
        *inner_argv,
    ]
    return argv
