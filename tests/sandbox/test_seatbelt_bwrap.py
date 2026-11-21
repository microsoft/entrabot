"""Profile and command generation for the two OS sandboxes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from entrabot.sandbox import bwrap, dbusproxy, seatbelt
from entrabot.sandbox.policy import ContainmentPolicy


def _policy(tmp_path: Path) -> ContainmentPolicy:
    home = tmp_path / "home"
    return ContainmentPolicy(
        home=home,
        # The running interpreter's environment, as the real policy has it: the enforcement
        # tests start it inside the sandbox, and it may sit on /Volumes.
        read_only=(tmp_path / "repo", tmp_path / "repo" / ".venv", Path(sys.prefix)),
        read_write=(home / ".entrabot", home / "Library" / "Caches" / "entrabot"),
        temp_dir=home / ".entrabot" / "tmp",
        allowed_hosts=("graph.microsoft.com",),
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths; Windows is not contained")
class TestSeatbeltProfile:
    def test_denies_home_writes_and_network_then_allows_back_only_the_policy(
        self, tmp_path: Path
    ) -> None:
        text = seatbelt.profile(_policy(tmp_path), proxy_port=54321)
        home = os.path.realpath(tmp_path / "home")
        order = [
            text.index("(allow default)"),
            text.index(f'(deny file-read* (subpath "{home}"))'),
            text.index("(allow file-read* "),
            text.index("(deny file-write*)"),
            text.index("(allow file-write* "),
            text.index("(deny network*)"),
            text.index('(allow network-outbound (remote ip "localhost:54321"))'),
        ]
        assert order == sorted(order)  # Seatbelt: the last matching rule wins
        allow_write = next(
            line for line in text.splitlines() if line.startswith("(allow file-write*")
        )
        assert os.path.realpath(tmp_path / "repo") not in allow_write
        assert os.path.realpath(tmp_path / "home" / ".entrabot") in allow_write
        assert f"{home}/Library/Keychains" in text  # the key store's client-side file

    def test_starting_programs_outside_and_scripting_other_apps_are_denied(
        self, tmp_path: Path
    ) -> None:
        # Run for real on macOS 27: from inside, "open -a Calculator" started an app, and an
        # app started by Launch Services runs outside the sandbox.
        text = seatbelt.profile(_policy(tmp_path), proxy_port=1)
        for rule in ("(deny lsopen)", "(deny appleevent-send)"):
            assert text.index(rule) > text.index("(allow default)")

    @pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS only")
    def test_launch_services_refuses_the_contained_process(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path)
        for path in (*policy.read_only, *policy.read_write):
            path.mkdir(parents=True, exist_ok=True)
        profile = tmp_path / "p.sb"
        profile.write_text(seatbelt.profile(policy, proxy_port=9, hidden_roots=(policy.home,)))
        result = subprocess.run(
            seatbelt.command(profile, ["/usr/bin/open", "-g", "-j", "-a", "Calculator"]),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, result.stdout + result.stderr

    def test_reads_are_also_denied_under_users_and_volumes(self, tmp_path: Path) -> None:
        # Other volumes (an external disk, a second data volume holding other projects) and
        # other users' folders are outside the home directory but just as private.
        text = seatbelt.profile(_policy(tmp_path), proxy_port=1)
        for root in ("/Users", "/Volumes"):
            deny = text.index(f'(deny file-read* (subpath "{os.path.realpath(root)}"))')
            assert deny < text.index("(allow file-read* ")

    def test_parents_of_allowed_paths_can_be_looked_at_but_not_listed(self, tmp_path: Path) -> None:
        text = seatbelt.profile(_policy(tmp_path), proxy_port=1)
        repo = Path(os.path.realpath(tmp_path / "repo"))
        for parent in repo.parents:
            assert f'(literal "{parent}")' in text
        metadata = next(
            line for line in text.splitlines() if line.startswith("(allow file-read-metadata")
        )
        assert "subpath" not in metadata

    def test_paths_are_quoted(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path)
        odd = ContainmentPolicy(
            home=policy.home,
            read_only=(Path('/Volumes/Example Disk/x"y'),),
            read_write=policy.read_write,
            temp_dir=policy.temp_dir,
            allowed_hosts=policy.allowed_hosts,
        )
        text = seatbelt.profile(odd, proxy_port=1)
        assert '(subpath "/Volumes/Example Disk/x\\"y")' in text

    @pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS only")
    def test_the_profile_compiles_and_is_enforced(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path)
        for path in (*policy.read_only, *policy.read_write):
            path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "home" / "secret.txt").write_text("secret")
        (tmp_path / "repo" / "code.py").write_text("x")
        profile = tmp_path / "p.sb"
        profile.write_text(seatbelt.profile(policy, proxy_port=9))
        probe = (
            "import socket,sys\n"
            "home,repo,state=sys.argv[1:4]\n"
            "def t(f):\n"
            "  try: f(); return 'ok'\n"
            "  except Exception: return 'deny'\n"
            "print(t(lambda: open(home+'/secret.txt').read()),"
            " t(lambda: open(repo+'/code.py').read()),"
            " t(lambda: open(state+'/w','w').write('x')),"
            " t(lambda: open(repo+'/w','w').write('x')),"
            " t(lambda: socket.create_connection(('1.1.1.1',443),timeout=3)))\n"
        )
        result = subprocess.run(
            seatbelt.command(
                profile,
                [
                    sys.executable,
                    "-I",
                    "-c",
                    probe,
                    str(tmp_path / "home"),
                    str(tmp_path / "repo"),
                    str(tmp_path / "home" / ".entrabot"),
                ],
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout.split() == ["deny", "ok", "ok", "deny", "deny"], result.stderr

    @pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS only")
    def test_a_hidden_root_outside_home_is_enforced(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path)
        for path in (*policy.read_only, *policy.read_write):
            path.mkdir(parents=True, exist_ok=True)
        other = tmp_path / "other-volume"
        other.mkdir()
        (other / "private.txt").write_text("private")
        (tmp_path / "repo" / "code.py").write_text("x")
        profile = tmp_path / "p.sb"
        profile.write_text(
            seatbelt.profile(policy, proxy_port=9, hidden_roots=(policy.home, other))
        )
        probe = (
            "import os,sys\n"
            "other,repo=sys.argv[1:3]\n"
            "def t(f):\n"
            "  try: f(); return 'ok'\n"
            "  except Exception: return 'deny'\n"
            "print(t(lambda: open(other+'/private.txt').read()), t(lambda: os.listdir(other)),"
            " t(lambda: open(repo+'/code.py').read()), t(lambda: os.path.realpath(repo)))\n"
        )
        result = subprocess.run(
            seatbelt.command(
                profile,
                [sys.executable, "-I", "-c", probe, str(other), str(tmp_path / "repo")],
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout.split() == ["deny", "deny", "ok", "ok"], result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths; Windows is not contained")
class TestBwrapCommand:
    def _command(self, tmp_path: Path, **kwargs: object) -> list[str]:
        return bwrap.command(
            _policy(tmp_path),
            bwrap="/usr/bin/bwrap",
            proxy_socket=Path("/tmp/eb/p.sock"),
            proxy_port=41000,
            inner_argv=["/venv/bin/python", "-m", "entrabot.mcp_server"],
            **kwargs,  # type: ignore[arg-type]
        )

    def test_shape(self, tmp_path: Path) -> None:
        argv = self._command(tmp_path)
        joined = " ".join(argv)
        for flag in (
            "--die-with-parent",
            "--new-session",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
        ):
            assert flag in argv
        assert f"--ro-bind-try {tmp_path / 'repo'} {tmp_path / 'repo'}" in joined
        assert f"--bind-try {tmp_path / 'home' / '.entrabot'}" in joined
        assert f"--ro-bind /tmp/eb/p.sock {bwrap.PROXY_SOCKET_INSIDE}" in joined
        tail = argv[argv.index("--") + 1 :]
        assert tail[:3] == ["/venv/bin/python", "-m", "entrabot.sandbox.bridge"]
        assert tail[-3:] == ["/venv/bin/python", "-m", "entrabot.mcp_server"]

    def test_the_host_root_is_never_mounted(self, tmp_path: Path) -> None:
        # Run for real on Ubuntu 24.04: with "--ro-bind / /" the server reached the user's
        # systemd over /run/user/<uid>/bus (a read-only mount does not stop connect() on a
        # Unix socket) and had it run a command outside the sandbox. Only named system
        # folders are mounted, so /run, /home, /mnt, /media, /var and /root do not exist.
        argv = self._command(tmp_path)
        binds = [
            (argv[i + 1], argv[i + 2])
            for i, flag in enumerate(argv)
            if flag in ("--ro-bind", "--ro-bind-try", "--bind", "--bind-try")
        ]
        sources = {source for source, _ in binds}
        assert "/" not in sources
        policy = _policy(tmp_path)
        # What the policy names is mounted wherever it is; nothing else is.
        sources -= {str(path) for path in (*policy.read_only, *policy.read_write)}
        sources -= {"/tmp/eb/p.sock"}
        for hidden in ("/run", "/home", "/mnt", "/media", "/var", "/root", "/srv", "/sys"):
            assert not any(
                source == hidden or source.startswith(hidden + "/") for source in sources
            ), hidden
        assert {"/usr", "/etc"} <= sources

    def test_the_root_ends_read_only(self, tmp_path: Path) -> None:
        # Without the host root, "/" is bubblewrap's own tmpfs: writable unless remounted.
        argv = self._command(tmp_path)
        boundary = argv.index("--")
        remount = max(i for i, flag in enumerate(argv[:boundary]) if flag == "--remount-ro")
        assert argv[remount + 1] == "/"
        last_mount = max(
            i
            for i, flag in enumerate(argv[:boundary])
            if flag in ("--ro-bind", "--ro-bind-try", "--bind", "--bind-try", "--tmpfs", "--dir")
        )
        assert remount > last_mount

    def test_the_session_bus_is_never_mounted_raw(self, tmp_path: Path) -> None:
        # The session bus carries systemd, portals and every desktop service. The server gets
        # the socket of a filtering proxy (entrabot.sandbox.dbusproxy) or no bus at all.
        without = self._command(tmp_path)
        mounts = without[: without.index("--")]
        assert bwrap.DBUS_SOCKET_INSIDE not in mounts
        assert not any(item.startswith("/run/") for item in mounts)
        with_proxy = self._command(tmp_path, dbus_proxy_socket=Path("/tmp/eb/dbus.sock"))
        joined = " ".join(with_proxy)
        assert f"--ro-bind /tmp/eb/dbus.sock {bwrap.DBUS_SOCKET_INSIDE}" in joined

    def test_no_capabilities_and_no_namespaces_of_its_own(self, tmp_path: Path) -> None:
        # A process that can make a user namespace can make a mount namespace in it and
        # rearrange what it sees. Flatpak closes this the same two ways: the flag, and the
        # system-call filter for a bubblewrap too old to have the flag.
        argv = self._command(tmp_path, disable_userns=True, seccomp_fd=9)
        joined = " ".join(argv[: argv.index("--")])
        assert "--unshare-user" in argv and "--disable-userns" in argv
        assert "--cap-drop ALL" in joined
        assert "--seccomp 9" in joined
        older = self._command(tmp_path)
        assert "--disable-userns" not in older and "--seccomp" not in older
        assert "--unshare-user" in older and "--cap-drop ALL" in " ".join(older)

    def test_links_of_a_merged_usr_stay_links(self, tmp_path: Path) -> None:
        # On a merged-/usr system /bin is a link to usr/bin. Mounting the link would mount
        # its target a second time; the same link is made inside instead.
        argv = self._command(tmp_path, system_links={"/bin": "usr/bin", "/lib64": "usr/lib64"})
        joined = " ".join(argv)
        assert "--symlink usr/bin /bin" in joined and "--symlink usr/lib64 /lib64" in joined
        assert "--ro-bind-try /bin /bin" not in joined
        assert "--ro-bind-try /usr /usr" in joined

    def test_the_interpreters_own_folder_is_mounted_read_only(self, tmp_path: Path) -> None:
        # A Python outside /usr (pyenv, Homebrew, /opt) would otherwise not start.
        argv = self._command(tmp_path, system_paths=(Path("/opt/python3.13"),))
        assert "--ro-bind-try /opt/python3.13 /opt/python3.13" in " ".join(argv)


class TestDbusProxyCommand:
    def test_filters_and_allows_only_the_key_store(self) -> None:
        argv = dbusproxy.command(
            "/usr/bin/xdg-dbus-proxy", "unix:path=/run/user/1000/bus", Path("/tmp/eb/d.sock"), 7
        )
        assert argv[:4] == [
            "/usr/bin/xdg-dbus-proxy",
            "--fd=7",
            "unix:path=/run/user/1000/bus",
            "/tmp/eb/d.sock",
        ]
        assert "--filter" in argv
        rules = [item for item in argv[4:] if item != "--filter"]
        assert rules and all(rule.startswith("--talk=") for rule in rules)
        assert "--talk=org.freedesktop.secrets" in rules
        # Nothing that reaches systemd, the portals or the bus at large.
        assert not any("systemd" in rule or "portal" in rule or "*" in rule for rule in rules)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_the_bridge_forwards_a_loopback_port_to_the_proxy_socket() -> None:
    import socket
    import tempfile
    import threading

    from entrabot.sandbox.bridge import serve

    path = Path(tempfile.mkdtemp(prefix="eb", dir="/tmp")) / "u.sock"
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.bind(str(path))
    upstream.listen(1)

    def echo() -> None:
        conn, _ = upstream.accept()
        conn.sendall(b"via-unix:" + conn.recv(64))
        conn.close()

    threading.Thread(target=echo, daemon=True).start()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=serve, args=(listener, str(path)), daemon=True).start()
    with socket.create_connection(listener.getsockname(), timeout=5) as client:
        client.sendall(b"hello")
        assert client.recv(64) == b"via-unix:hello"
    listener.close()
    upstream.close()
