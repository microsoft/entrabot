"""Microsoft eXecution Container (MXC) as the containment backend, when it is installed.

From MXC v1.0.1 (github.com/microsoft/mxc, read at the tag; see the shared research notes):

- The executors are ``mxc-exec-mac`` (macOS, Apple Silicon only in the release zip) and
  ``lxc-exec`` (Linux, bubblewrap). They take the request as a file or ``--config-base64``,
  not on standard input, and pass the workload's standard input and output through, so a
  stdio MCP server can run inside. ``--debug`` writes to standard output, so it is never used.
- The 1.0.0 request has no hostname rules ("not hostname filtering"), so egress is denied
  with no rules and ``runtimeConfig.networkProxy`` points at the launcher's allowlisting
  proxy, which holds the host list. ``process.commandLine`` is one string run through
  ``sh -c``. ``process.timeout: 0`` is no timeout.
- On Seatbelt, ``seatbelt.keychainAccess`` opens the Mach services and ``~/Library/Keychains``
  the certificate key needs. On bubblewrap nothing documents the Secret Service; the runtime
  directory's bus socket is added read-write (inferred, untested).

Used only when the executor is found and passes :func:`_trusted` (owned by this user or
root, not writable by anyone else; ``ENTRABOT_MXC_SHA256``, when set, must match). Not run
on a real system by this change: no executor was installed on the machine it was written on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from entrabot.sandbox.policy import ContainmentPolicy

EXECUTOR_ENV = "ENTRABOT_MXC_EXECUTOR"
SHA256_ENV = "ENTRABOT_MXC_SHA256"
REQUEST_VERSION = "1.0.0"
# What the interpreter and the system need to start: MXC denies the filesystem by default.
_SYSTEM_READ = ("/usr", "/bin", "/lib", "/lib64", "/etc", "/System", "/Library", "/private/etc")


def executor_name(platform: str = sys.platform) -> str | None:
    if platform == "darwin":
        return "mxc-exec-mac"
    if platform.startswith("linux"):
        return "lxc-exec"
    return None


def _trusted(path: str, expected_sha256: str | None) -> bool:
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # Windows: no owner check, so never trusted (MXC is not used there)
        return False
    try:
        info = os.stat(path)
    except OSError:
        return False
    if info.st_uid not in (getuid(), 0) or info.st_mode & 0o022:
        return False
    if expected_sha256:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return digest.lower() == expected_sha256.strip().lower()
    return True


def find_executor(
    *,
    env: Mapping[str, str] | None = None,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """The MXC executor to use, or None: ``ENTRABOT_MXC_EXECUTOR``, else the one on PATH."""
    env = os.environ if env is None else env
    name = executor_name(platform)
    if name is None:
        return None
    candidate = env.get(EXECUTOR_ENV) or which(name)
    if candidate and _trusted(candidate, env.get(SHA256_ENV)):
        return candidate
    return None


def request(
    policy: ContainmentPolicy,
    *,
    proxy_port: int,
    inner_argv: list[str],
    env: Mapping[str, str],
    platform: str = sys.platform,
) -> dict:
    """The MXC 1.0.0 container-creation request for the server."""
    read_only = [str(path) for path in policy.read_only]
    read_only += [sys.base_prefix, *(p for p in _SYSTEM_READ if os.path.exists(p))]
    read_write = [str(path) for path in policy.read_write]
    if platform.startswith("linux"):
        runtime = env.get("XDG_RUNTIME_DIR")
        if runtime:
            read_write.append(str(Path(runtime) / "bus"))
    body: dict = {
        "version": REQUEST_VERSION,
        "containment": "process",
        "process": {
            "commandLine": shlex.join(inner_argv),
            "cwd": str(policy.temp_dir),
            "env": [f"{key}={value}" for key, value in env.items()],
            "inheritDefaultEnv": False,
            "timeout": 0,
        },
        "filesystem": {
            "readonlyPaths": list(dict.fromkeys(read_only)),
            "readwritePaths": list(dict.fromkeys(read_write)),
        },
        "network": {
            "egress": {"default": "deny"},
            "ingress": {"default": "deny", "hostLoopback": "deny"},
        },
        "runtimeConfig": {"networkProxy": f"http://127.0.0.1:{int(proxy_port)}"},
    }
    if platform == "darwin":
        body["seatbelt"] = {"keychainAccess": True}
    return body


def command(
    policy: ContainmentPolicy,
    *,
    proxy_port: int,
    inner_argv: list[str],
    env: Mapping[str, str],
    executor: str | None = None,
) -> list[str]:
    executor = executor or find_executor()
    if executor is None:
        raise RuntimeError("no trusted MXC executor")
    encoded = base64.b64encode(
        json.dumps(request(policy, proxy_port=proxy_port, inner_argv=inner_argv, env=env)).encode()
    ).decode()
    return [executor, "--config-base64", encoded]
