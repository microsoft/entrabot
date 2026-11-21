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
  on standard error, and record it, so ``./status.sh`` and ``--diagnose`` report it. On Linux
  "available" means ``bwrap`` is installed and a trial run of it works: some systems (stock
  Ubuntu 24.04, through AppArmor) stop unprivileged user namespaces, and ``bwrap`` exits 1.
- ``required``: refuse to start uncontained (exit 2).
- ``off``: never contain.
- ``seatbelt``, ``bwrap``, ``mxc``: that sandbox or nothing (exit 2 when it is unavailable).

While the server runs, ``<state>/runtime/containment.json`` says how it is contained.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform as platform_module
import re
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


BWRAP_TRIAL_SECONDS = 10
BWRAP_REFUSED = (
    "bubblewrap (bwrap) is installed but this system does not let it create a sandbox: "
    "unprivileged user namespaces are restricted (on Ubuntu 24.04, by AppArmor)"
)


BWRAP_DISABLE_USERNS = (0, 8, 0)  # the first bubblewrap with --disable-userns
NO_SECCOMP = (
    "there is no system-call filter for this kind of machine (x86-64 and arm64 have one), "
    "and the Linux sandbox is not used without it"
)


def bwrap_version(bwrap: str) -> tuple[int, int, int]:
    """The version ``bwrap --version`` prints, or ``(0, 0, 0)`` when it cannot be read."""
    try:
        done = subprocess.run(  # noqa: S603 - bwrap by full path, fixed arguments
            [bwrap, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=BWRAP_TRIAL_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return (0, 0, 0)
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", done.stdout or "")
    return (int(found[1]), int(found[2]), int(found[3])) if found else (0, 0, 0)


def bwrap_works(bwrap: str) -> bool:
    """Can *bwrap* create the namespaces the server's sandbox needs? A trial run of ``true``
    with the same namespaces as :func:`entrabot.sandbox.bwrap.command`. Where unprivileged
    user namespaces are restricted it exits 1; without this check the server would not start."""
    trial = [
        bwrap,
        "--unshare-user",
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
        "true",
    ]
    try:
        done = subprocess.run(  # noqa: S603 - bwrap by full path, fixed arguments
            trial,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=BWRAP_TRIAL_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


DBUS_PROXY_MISSING = (
    "xdg-dbus-proxy is not installed: the contained server reaches the key store through it, "
    "because the session bus itself (which carries systemd) is never given to the sandbox"
)

EXTRA_ENV = "ENTRABOT_SANDBOX_EXTRA_ENV"
# The contained server gets these and nothing else from the launcher's environment. Whatever
# the shell or the MCP host that started Entrabot holds (tokens for other services, agent
# sockets, the session bus address) stays outside.
ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TERM",
        "NO_COLOR",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "COPILOT_HOME",
        "CLAUDE_PROJECT_DIR",
        "WAIT_TOOL_POLL_INTERVAL_S",
        "WAIT_TOOL_HEARTBEAT_S",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "__CF_USER_TEXT_ENCODING",
    }
)
ENV_PREFIXES = (
    "LC_",
    "ENTRABOT_",
    "PERSONA_SATI_",
    "EFFERENT_COPY_",
    "OTEL_",
    "MICROSOFT_OTEL_",
    "APPLICATIONINSIGHTS_",
)


def child_env(parent: Mapping[str, str]) -> dict[str, str]:
    """The part of *parent* the contained server gets: :data:`ENV_NAMES`, the names starting
    with :data:`ENV_PREFIXES`, and the names listed in ``ENTRABOT_SANDBOX_EXTRA_ENV``."""
    extra = {name.strip() for name in parent.get(EXTRA_ENV, "").split(",") if name.strip()}
    return {
        name: value
        for name, value in parent.items()
        if name in ENV_NAMES or name in extra or name.startswith(ENV_PREFIXES)
    }


def choose(
    *,
    platform: str,
    env: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
    mxc_executor: Callable[[], str | None] | None = None,
    bwrap_works: Callable[[str], bool] = bwrap_works,
    machine: str | None = None,
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
    bwrap_problem = "bubblewrap (bwrap) is not installed"
    if mode in ("auto", "required", "bwrap"):
        bwrap = which("bwrap") if platform.startswith("linux") else None
        if bwrap and not available.get("mxc"):
            if not bwrap_works(bwrap):
                bwrap_problem, bwrap = BWRAP_REFUSED, None
            elif not which("xdg-dbus-proxy"):
                bwrap_problem, bwrap = DBUS_PROXY_MISSING, None
            else:
                from entrabot.sandbox import seccomp

                if seccomp.program(machine or platform_module.machine()) is None:
                    bwrap_problem, bwrap = NO_SECCOMP, None
        available["bwrap"] = bwrap
    for backend in ("mxc", "seatbelt", "bwrap"):
        if available.get(backend):
            return Choice(backend, f"{backend} found")
    if platform == "win32":
        why = "Windows is not contained yet"
    elif platform.startswith("linux"):
        why = bwrap_problem
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

    from entrabot.sandbox import bwrap, dbusproxy, mxc, seatbelt, seccomp
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
    env = child_env(os.environ)
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
    dbus_socket: Path | None = None
    dbus_proxy: dbusproxy.DbusProxy | None = None
    profile_path: Path | None = None
    inherited: tuple[int, ...] = ()
    if backend == "bwrap":
        socket_path = _short_socket_path()
        proxy.start_unix(socket_path)
        port = 3128  # inside the namespace; the bridge forwards it to the socket
        use_proxy(port)
        bus = bwrap.session_bus_address(dict(os.environ))
        if bus:
            # The key store only, through a filtering proxy; never the session bus itself.
            dbus_socket = socket_path.parent / "dbus.sock"
            dbus_proxy = dbusproxy.DbusProxy()
            dbus_proxy.start(
                shutil.which(dbusproxy.EXECUTABLE) or dbusproxy.EXECUTABLE, bus, dbus_socket
            )
            env["DBUS_SESSION_BUS_ADDRESS"] = bwrap.DBUS_ADDRESS_INSIDE
        # A Python that is not under the system folders (pyenv, Homebrew, /opt).
        interpreter_home = Path(os.path.realpath(sys.base_prefix))
        outside_system = not any(
            interpreter_home.is_relative_to(root) for root in bwrap.SYSTEM_PATHS
        )
        bwrap_path = shutil.which("bwrap") or "bwrap"
        # The system-call filter, in a file only this process and bwrap hold.
        filter_program = seccomp.program(platform_module.machine())
        if filter_program is None:
            raise SystemExit(f"[entrabot] {NO_SECCOMP}.")
        seccomp_fd = os.memfd_create("entrabot-seccomp")  # type: ignore[attr-defined]
        os.write(seccomp_fd, filter_program)
        os.lseek(seccomp_fd, 0, os.SEEK_SET)
        inherited = (seccomp_fd,)
        command = bwrap.command(
            policy,
            bwrap=bwrap_path,
            proxy_socket=socket_path,
            proxy_port=port,
            inner_argv=inner,
            dbus_proxy_socket=dbus_socket,
            system_paths=(interpreter_home,) if outside_system else (),
            system_links=bwrap.system_links(),
            disable_userns=bwrap_version(bwrap_path) >= BWRAP_DISABLE_USERNS,
            seccomp_fd=seccomp_fd,
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

    child = subprocess.Popen(command, env=env, pass_fds=inherited)
    for fd in inherited:
        os.close(fd)
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
        if dbus_proxy is not None:
            dbus_proxy.stop()
        if record_status:
            _clear_status(state_root, child.pid)
        if dbus_socket is not None:
            with contextlib.suppress(OSError):
                dbus_socket.unlink()
        if socket_path is not None:
            with contextlib.suppress(OSError):
                socket_path.unlink()
                socket_path.parent.rmdir()
        if profile_path is not None:
            with contextlib.suppress(OSError):
                profile_path.unlink()
