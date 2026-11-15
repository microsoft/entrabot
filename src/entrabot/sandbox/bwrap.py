"""The Linux bubblewrap command for the contained server.

- The whole system is mounted read-only (``--ro-bind / /``), the home directory is replaced
  by an empty tmpfs, and the repository, the virtual environment and Entrabot's state are
  mounted back: the first two read-only, the state read-write. ``/tmp`` is a private tmpfs.
- The server gets its own network namespace (``--unshare-net``): no interface but loopback.
  The launcher's proxy listens on a Unix socket, which is mounted in, and
  :mod:`entrabot.sandbox.bridge` forwards a loopback port inside the namespace to it.
- The Secret Service (GNOME Keyring, KWallet) is reached over the D-Bus session bus. Its
  socket is mounted in when ``DBUS_SESSION_BUS_ADDRESS`` names a path; an abstract socket
  belongs to the host's network namespace and cannot be reached from inside, so a key store
  on one stops the sign-in (it fails closed, as an unreadable key store always does).
- ``--die-with-parent``, ``--new-session`` (no terminal injection), ``--unshare-pid``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from entrabot.sandbox.policy import ContainmentPolicy

# In the private /tmp: bwrap can create the mount point there.
PROXY_SOCKET_INSIDE = "/tmp/entrabot-proxy.sock"


def session_bus_path(env: Mapping[str, str]) -> str | None:
    """The filesystem path of the D-Bus session bus, or None (unset, or abstract)."""
    for part in env.get("DBUS_SESSION_BUS_ADDRESS", "").split(";"):
        if not part.startswith("unix:"):
            continue
        for item in part[len("unix:") :].split(","):
            key, _, value = item.partition("=")
            if key == "path" and value:
                return value
    return None


def command(
    policy: ContainmentPolicy,
    *,
    bwrap: str,
    proxy_socket: Path,
    proxy_port: int,
    inner_argv: list[str],
    env: Mapping[str, str],
) -> list[str]:
    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        str(policy.home),
    ]
    for path in policy.read_only:
        argv += ["--ro-bind-try", str(path), str(path)]
    for path in policy.read_write:
        argv += ["--bind-try", str(path), str(path)]
    argv += ["--ro-bind", str(proxy_socket), PROXY_SOCKET_INSIDE]
    bus = session_bus_path(env)
    if bus:
        argv += ["--bind-try", bus, bus]
        keyring_dir = str(Path(bus).parent / "keyring")
        argv += ["--bind-try", keyring_dir, keyring_dir]
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
