"""The Linux sandbox, run for real: things a hostile process inside would try.

These start ``bwrap`` with exactly the command the server gets and attack it from inside.
They exist because the first version of this sandbox was tested only for the shape of its
command line, and was escaped the first time it was attacked: over the session bus, the
user's systemd ran a command outside it.

They run only on Linux, where ``bwrap`` is installed and the system lets it create a
sandbox (on Ubuntu 24.04 that needs an AppArmor profile for ``bwrap``); elsewhere they skip.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from entrabot.sandbox import bwrap, launch, seccomp
from entrabot.sandbox.policy import ContainmentPolicy

BWRAP = shutil.which("bwrap") if sys.platform.startswith("linux") else None
pytestmark = pytest.mark.skipif(
    not (BWRAP and launch.bwrap_works(BWRAP)),
    reason="needs Linux with a bubblewrap the system lets create a sandbox",
)

PROBE = r"""
import json, os, socket, stat, subprocess, sys
home, repo, state, marker = sys.argv[1:5]
uid = os.getuid()
out = {}
def attempt(name, action):
    try:
        out[name] = {"ok": True, "value": action()}
    except Exception as exc:
        out[name] = {"ok": False, "error": type(exc).__name__}
def connect(path):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(path)
    return "connected"
def run(command):
    done = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return done.returncode
def write(path):
    with open(path, "w") as handle:
        handle.write("x")
    os.unlink(path)
    return "written"
for folder in ("/run", "/home", "/mnt", "/media", "/var", "/root", "/srv", "/sys"):
    attempt("list " + folder, lambda folder=folder: sorted(os.listdir(folder)))
attempt("list home", lambda: sorted(os.listdir(home)))
beside = os.path.dirname(repo) + "/private.txt"
attempt("read file beside the repository", lambda: open(beside).read())
attempt("read repository", lambda: open(repo + "/code.py").read())
attempt("write repository", lambda: write(repo + "/probe"))
attempt("write home", lambda: write(home + "/probe"))
attempt("write root", lambda: write("/probe"))
attempt("write usr", lambda: write("/usr/probe"))
attempt("write state", lambda: write(state + "/probe"))
attempt("session bus", lambda: connect("/run/user/%d/bus" % uid))
attempt("systemd private socket", lambda: connect("/run/user/%d/systemd/private" % uid))
attempt("system bus", lambda: connect("/run/dbus/system_bus_socket"))
attempt("docker socket", lambda: connect("/run/docker.sock"))
def sockets():
    found = []
    skip = {"/proc", "/dev", "/usr", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32"}
    for top, folders, files in os.walk("/"):
        folders[:] = [f for f in folders if os.path.join(top, f) not in skip]
        if "site-packages" in folders:
            folders.remove("site-packages")
        for name in files + folders:
            path = os.path.join(top, name)
            if stat.S_ISSOCK(os.lstat(path).st_mode) and not path.startswith("/tmp/entrabot-"):
                found.append(path)
    return sorted(found)
attempt("host sockets visible", sockets)
escape = ("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%d/bus XDG_RUNTIME_DIR=/run/user/%d "
          "systemd-run --user --wait --collect /bin/sh -c 'echo out > %s'" % (uid, uid, marker))
attempt("systemd-run exit code", lambda: run(escape))
direct = lambda: socket.create_connection(("1.1.1.1", 443), timeout=4) and "connected"
attempt("direct connection", direct)
attempt("name lookup", lambda: socket.getaddrinfo("example.com", 443) and "resolved")
attempt("host processes", lambda: len([name for name in os.listdir("/proc") if name.isdigit()]))
attempt("environment", lambda: sorted(os.environ))
attempt("new user namespace", lambda: os.unshare(os.CLONE_NEWUSER) or "made")
attempt("unshare -U", lambda: run("unshare -U true"))
attempt("unshare -m", lambda: run("unshare -m true"))
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
def keyring():
    number = {"x86_64": 250, "aarch64": 219}[os.uname().machine]
    result = libc.syscall(number, 0, -3, 0)  # KEYCTL_GET_KEYRING_ID of the session keyring
    if result < 0:
        raise OSError(ctypes.get_errno(), "keyctl")
    return result
attempt("kernel keyring", keyring)
attempt("capabilities", lambda: [
    line.split()[1] for line in open("/proc/self/status") if line.startswith(("CapEff", "CapPrm"))])
attempt("no new privileges", lambda: [
    line.split()[1] for line in open("/proc/self/status") if line.startswith("NoNewPrivs")])
print(json.dumps(out))
"""


@pytest.fixture
def sandbox() -> Iterator[dict]:
    # Under the real home directory: /tmp is replaced inside the sandbox, and a policy path
    # there would be mounted over the private one, which the real policy never does.
    root = Path(tempfile.mkdtemp(prefix=".entrabot-sandbox-test-", dir=Path.home()))
    home = Path.home()
    repo = root / "repo"
    state = root / "state"
    for folder in (repo, state):
        folder.mkdir()
    (repo / "code.py").write_text("code")
    (root / "private.txt").write_text("private")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    proxy_socket = root / "proxy.sock"
    listener.bind(str(proxy_socket))
    listener.listen(1)
    # The real project and its environment too: the bridge and the probe start from them.
    project = Path(launch.__file__).resolve().parents[3]
    policy = ContainmentPolicy(
        home=home,
        read_only=(repo, project, Path(sys.prefix)),
        read_write=(state,),
        temp_dir=state / "tmp",
        allowed_hosts=(),
    )
    try:
        yield {
            "policy": policy,
            "proxy_socket": proxy_socket,
            "repo": repo,
            "state": state,
            "marker": root / "ESCAPED",
            "home": home,
        }
    finally:
        listener.close()
        shutil.rmtree(root, ignore_errors=True)


def _run_probe(sandbox: dict) -> dict:
    interpreter_home = Path(os.path.realpath(sys.base_prefix))
    outside_system = not any(interpreter_home.is_relative_to(p) for p in bwrap.SYSTEM_PATHS)
    filter_program = seccomp.program(platform.machine())
    assert filter_program is not None
    seccomp_fd = os.memfd_create("test-seccomp")
    os.write(seccomp_fd, filter_program)
    os.lseek(seccomp_fd, 0, os.SEEK_SET)
    command = bwrap.command(
        sandbox["policy"],
        bwrap=str(BWRAP),
        proxy_socket=sandbox["proxy_socket"],
        proxy_port=3128,
        inner_argv=[
            sys.executable,
            "-I",
            "-c",
            PROBE,
            str(sandbox["home"]),
            str(sandbox["repo"]),
            str(sandbox["state"]),
            str(sandbox["marker"]),
        ],
        system_paths=(interpreter_home,) if outside_system else (),
        system_links=bwrap.system_links(),
        disable_userns=launch.bwrap_version(str(BWRAP)) >= launch.BWRAP_DISABLE_USERNS,
        seccomp_fd=seccomp_fd,
    )
    env = launch.child_env({**os.environ, "A_TOKEN_IN_THE_SHELL": "secret"})
    done = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=120, pass_fds=(seccomp_fd,)
    )
    os.close(seccomp_fd)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_users_systemd_cannot_be_asked_to_run_a_command_outside(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    assert not sandbox["marker"].exists(), "a command ran outside the sandbox"
    escape = report["systemd-run exit code"]
    assert not escape["ok"] or escape["value"] != 0
    for name in ("session bus", "systemd private socket", "system bus", "docker socket"):
        assert not report[name]["ok"], name


def test_no_socket_of_the_host_is_reachable(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    assert report["host sockets visible"] == {"ok": True, "value": []}


def test_only_the_policys_folders_exist(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    for folder in ("/run", "/mnt", "/media", "/var", "/root", "/srv", "/sys"):
        assert not report["list " + folder]["ok"], folder
    # /home exists only as the way to the folders mounted back: no other user's folder.
    assert report["list /home"]["value"] == [sandbox["home"].name]
    # In the home directory: the way to each mounted folder, and nothing that was there.
    policy = sandbox["policy"]
    expected = {
        path.relative_to(sandbox["home"]).parts[0]
        for path in (*policy.read_only, *policy.read_write)
        if path.is_relative_to(sandbox["home"])
    }
    assert set(report["list home"]["value"]) == expected
    assert not report["read file beside the repository"]["ok"]
    assert report["read repository"] == {"ok": True, "value": "code"}


def test_nothing_is_writable_but_the_state(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    for name in ("write repository", "write home", "write root", "write usr"):
        assert not report[name]["ok"], name
    assert report["write state"] == {"ok": True, "value": "written"}


def test_no_network_and_no_view_of_other_processes(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    assert not report["direct connection"]["ok"]
    assert not report["name lookup"]["ok"]
    assert report["host processes"]["value"] <= 4  # bwrap, the bridge, the probe


def test_the_shells_environment_stays_outside(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    names = report["environment"]["value"]
    assert "A_TOKEN_IN_THE_SHELL" not in names
    assert "DBUS_SESSION_BUS_ADDRESS" not in names and "XDG_RUNTIME_DIR" not in names
    assert "SSH_AUTH_SOCK" not in names


def test_it_cannot_make_namespaces_of_its_own(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    assert not report["new user namespace"]["ok"]
    for name in ("unshare -U", "unshare -m"):
        assert not report[name]["ok"] or report[name]["value"] != 0, name


def test_the_kernel_keyring_is_out_of_reach(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    assert report["kernel keyring"] == {"ok": False, "error": "PermissionError"}


def test_it_holds_no_capabilities_and_cannot_gain_any(sandbox: dict) -> None:
    report = _run_probe(sandbox)
    assert set(report["capabilities"]["value"]) == {"0000000000000000"}
    assert report["no new privileges"]["value"] == ["1"]
