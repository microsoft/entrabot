"""Start the Entrabot MCP server inside an OS sandbox, by default.

``entrabot-mcp`` calls :func:`maybe_contain` first. Unless the process is already the
contained server, it becomes the launcher: it starts the allowlisting proxy
(:mod:`entrabot.sandbox.proxy`), starts the server again inside the sandbox with the same
standard input and output (the MCP host does not notice), forwards signals to it, and exits
with its exit code.

Which sandbox, in this order: Microsoft eXecution Container when its executor is installed
and verified (:mod:`entrabot.sandbox.mxc`), ``sandbox-exec`` with a generated Seatbelt profile
on macOS, ``bwrap`` on Linux. Windows is not contained yet.

``ENTRABOT_CONTAINMENT`` (environment or ``.env``):

- ``auto`` (default): contain when a sandbox is available; otherwise run uncontained, say so
  on standard error, and record it, so ``./status.sh`` and ``--diagnose`` report it.
- ``required``: refuse to start uncontained (exit 2).
- ``off``: never contain.
- ``seatbelt``, ``bwrap``, ``mxc``: that sandbox or nothing (exit 2 when it is unavailable).

While the server runs, ``<state>/runtime/containment.json`` says how it is contained.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

CONTAINED_ENV = "ENTRABOT_CONTAINED"
MODE_ENV = "ENTRABOT_CONTAINMENT"
MODES = ("auto", "required", "off", "seatbelt", "bwrap", "mxc")
STATUS_FILE = Path("runtime") / "containment.json"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"


@dataclass(frozen=True)
class Choice:
    backend: str | None  # "mxc", "seatbelt", "bwrap", or None: uncontained
    reason: str


def choose(
    *,
    platform: str,
    env: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
    mxc_executor: Callable[[], str | None] | None = None,
) -> Choice:
    """Which sandbox to use. A mode that cannot be honoured raises :class:`SystemExit`."""
    mode = (env.get(MODE_ENV) or "auto").strip().lower()
    if mode not in MODES:
        raise SystemExit(f"[entrabot] {MODE_ENV}={mode!r} is not one of {', '.join(MODES)}")
    if mode == "off":
        return Choice(None, f"{MODE_ENV}=off")
    if mxc_executor is None:
        from entrabot.sandbox.mxc import find_executor as mxc_executor
    available: dict[str, str | None] = {}
    if mode in ("auto", "required", "mxc"):
        available["mxc"] = mxc_executor() if platform in ("darwin", "linux") else None
    if mode in ("auto", "required", "seatbelt"):
        available["seatbelt"] = (
            SANDBOX_EXEC if platform == "darwin" and exists(SANDBOX_EXEC) else None
        )
    if mode in ("auto", "required", "bwrap"):
        available["bwrap"] = which("bwrap") if platform.startswith("linux") else None
    for backend in ("mxc", "seatbelt", "bwrap"):
        if available.get(backend):
            return Choice(backend, f"{backend} found")
    if platform == "win32":
        why = "Windows is not contained yet"
    elif platform.startswith("linux"):
        why = "bubblewrap (bwrap) is not installed"
    else:
        why = "no sandbox available"
    if mode == "auto":
        return Choice(None, why)
    raise SystemExit(f"[entrabot] {MODE_ENV}={mode} but {why}; refusing to start uncontained.")


def _status_path(state_root: Path) -> Path:
    return state_root / STATUS_FILE


def write_status(state_root: Path, record: dict) -> None:
    path = _status_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_status(state_root: Path) -> dict | None:
    """The record of the running server, or None when there is none or its process is gone."""
    try:
        record = json.loads(_status_path(state_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or not _alive(pid):
        return None
    return record


def describe_running(state_root: Path | None = None) -> tuple[str, str]:
    """``(status, detail)`` for status and diagnose: "pass" when the running server is
    contained, "warn" when it runs without containment, "skip" when no server is running."""
    root = state_root if state_root is not None else Path.home() / ".entrabot"
    record = read_status(root)
    if record is None:
        holder = _lock_holder()
        if holder is not None:
            return (
                "warn",
                f"an Entrabot server is running (PID {holder}) but recorded no containment: it "
                "was started by a version without it, so it is NOT contained. Restart it.",
            )
        return "skip", "no running Entrabot server is recorded on this computer"
    backend = record.get("backend")
    if backend and backend != "none":
        hosts = ", ".join(record.get("allowed_hosts") or [])
        return "pass", f"the running server (PID {record['pid']}) is contained by {backend}; " + (
            f"it can reach only {hosts}" if hosts else "network through its proxy only"
        )
    return (
        "warn",
        f"the running server (PID {record['pid']}) is NOT contained: {record.get('reason')}",
    )


def _lock_holder() -> int | None:
    """The PID holding the server's single-instance lock, when that process is alive."""
    try:
        from entrabot.singleton import default_lock_path

        lock = default_lock_path()
        pid = int(lock.with_suffix(lock.suffix + ".holder.pid").read_text().strip())
    except (OSError, ValueError, ImportError):
        return None
    return pid if _alive(pid) else None


def _alive(pid: int) -> bool:
    """Is process *pid* running? Never sends a signal."""
    if pid <= 0:  # 0 and -1 name process groups, not one process
        return False
    if sys.platform == "win32":
        # Signal 0 is CTRL_C_EVENT on Windows: os.kill(pid, 0) would send Ctrl+C.
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _alive_windows(pid: int) -> bool:
    """Is process *pid* running? Windows only: asks for its exit code, signals nothing."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    except (ctypes.ArgumentError, OverflowError):
        return False
    if not handle:
        # Another user's process can't be opened, but it exists.
        return ctypes.get_last_error() == error_access_denied  # type: ignore[attr-defined]
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _forwarded_signals() -> tuple[int, ...]:
    """The signals the launcher passes on to the contained server: those this system has
    (Windows has no SIGHUP)."""
    names = ("SIGTERM", "SIGINT", "SIGHUP")
    return tuple(getattr(signal, name) for name in names if hasattr(signal, name))


def _clear_status(state_root: Path, pid: int) -> None:
    current = read_status(state_root)
    if current is None or current.get("pid") == pid:
        with contextlib.suppress(OSError):
            _status_path(state_root).unlink()


def _short_socket_path() -> Path:
    # A Unix socket path is limited to about 100 bytes; the state directory may be longer.
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(tempfile.mkdtemp(prefix="entrabot-", dir=base)) / "proxy.sock"


def maybe_contain(argv: list[str] | None = None) -> int | None:
    """Run the server contained and return its exit code, or None when this process should
    go on to be the server itself (it already is the contained one, or it runs uncontained)."""
    if os.environ.get(CONTAINED_ENV):
        return None
    from entrabot.config import get_config

    config = get_config()
    home = Path.home()
    state_root = home / ".entrabot"
    try:
        choice = choose(platform=sys.platform, env=os.environ)
    except SystemExit as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    if choice.backend is None:
        os.environ[CONTAINED_ENV] = "none"
        sys.stderr.write(f"[entrabot] running WITHOUT containment: {choice.reason}.\n")
        with contextlib.suppress(OSError):
            write_status(
                state_root,
                {
                    "backend": "none",
                    "reason": choice.reason,
                    "pid": os.getpid(),
                    "started_at": time.time(),
                },
            )
        return None
    inner = [sys.executable, "-m", "entrabot.mcp_server", *(argv or sys.argv[1:])]
    return run_contained(choice.backend, config, state_root, home, inner)


def run_contained(
    backend: str,
    config,
    state_root: Path,
    home: Path,
    inner: list[str],
    *,
    record_status: bool = True,
) -> int:
    """Run *inner* contained by *backend* behind the allowlisting proxy; its exit code."""
    import platformdirs

    from entrabot.sandbox import bwrap, mxc, seatbelt
    from entrabot.sandbox.policy import build_policy
    from entrabot.sandbox.proxy import AllowlistProxy

    project_root = Path(__file__).resolve().parents[3]
    venv = Path(sys.prefix)
    policy = build_policy(
        config,
        project_root=project_root,
        venv=venv,
        home=home,
        cache_dir=Path(platformdirs.user_cache_dir("entrabot")),
        env=os.environ,
    )
    for path in (*policy.read_write, policy.temp_dir):
        path.mkdir(parents=True, exist_ok=True)
    refusals = Path(config.log_dir) / "containment-refusals.jsonl"

    def record_refusal(what: str) -> None:
        with refusals.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "refused": what}) + "\n")

    proxy = AllowlistProxy(policy.allowed_hosts, on_refusal=record_refusal)
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("NO_PROXY",)}
    env.update(
        {
            CONTAINED_ENV: backend,
            "TMPDIR": str(policy.temp_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    def use_proxy(port: int) -> None:
        url = f"http://127.0.0.1:{port}"
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            env[key] = url
            env[key.lower()] = url

    socket_path: Path | None = None
    profile_path: Path | None = None
    if backend == "bwrap":
        socket_path = _short_socket_path()
        proxy.start_unix(socket_path)
        port = 3128  # inside the namespace; the bridge forwards it to the socket
        use_proxy(port)
        command = bwrap.command(
            policy,
            bwrap=shutil.which("bwrap") or "bwrap",
            proxy_socket=socket_path,
            proxy_port=port,
            inner_argv=inner,
            env=os.environ,
        )
    else:
        port = proxy.start_tcp()
        use_proxy(port)
        if backend == "seatbelt":
            profile_path = policy.temp_dir / f"seatbelt-{os.getpid()}.sb"
            profile_path.write_text(seatbelt.profile(policy, proxy_port=port), encoding="utf-8")
            command = seatbelt.command(profile_path, inner)
        else:
            command = mxc.command(policy, proxy_port=port, inner_argv=inner, env=env)

    child = subprocess.Popen(command, env=env)
    if record_status:
        write_status(
            state_root,
            {
                "backend": backend,
                "pid": child.pid,
                "launcher_pid": os.getpid(),
                "started_at": time.time(),
                "allowed_hosts": list(policy.allowed_hosts),
                "read_write": [str(path) for path in policy.read_write],
                "read_only": [str(path) for path in policy.read_only],
            },
        )

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    for sig in _forwarded_signals():
        signal.signal(sig, forward)
    try:
        return child.wait()
    finally:
        proxy.stop()
        if record_status:
            _clear_status(state_root, child.pid)
        if socket_path is not None:
            with contextlib.suppress(OSError):
                socket_path.unlink()
                socket_path.parent.rmdir()
        if profile_path is not None:
            with contextlib.suppress(OSError):
                profile_path.unlink()
