"""Profile and command generation for the two OS sandboxes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from entrabot.sandbox import bwrap, seatbelt
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

    def test_reads_are_also_denied_under_users_and_volumes(self, tmp_path: Path) -> None:
        # Other volumes (an external disk, a second data volume holding other projects) and
        # other users' folders are outside the home directory but just as private.
        text = seatbelt.profile(_policy(tmp_path), proxy_port=1)
        for root in ("/Users", "/Volumes"):
            deny = text.index(f'(deny file-read* (subpath "{os.path.realpath(root)}"))')
            assert deny < text.index("(allow file-read* ")

    def test_parents_of_allowed_paths_can_be_looked_at_but_not_listed(
        self, tmp_path: Path
    ) -> None:
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
            read_only=(Path('/Volumes/Development HD/x"y'),),
            read_write=policy.read_write,
            temp_dir=policy.temp_dir,
            allowed_hosts=policy.allowed_hosts,
        )
        text = seatbelt.profile(odd, proxy_port=1)
        assert '(subpath "/Volumes/Development HD/x\\"y")' in text

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


class TestBwrapCommand:
    def _command(self, tmp_path: Path, env: dict[str, str]) -> list[str]:
        return bwrap.command(
            _policy(tmp_path),
            bwrap="/usr/bin/bwrap",
            proxy_socket=Path("/tmp/eb/p.sock"),
            proxy_port=41000,
            inner_argv=["/venv/bin/python", "-m", "entrabot.mcp_server"],
            env=env,
        )

    def test_shape(self, tmp_path: Path) -> None:
        argv = self._command(tmp_path, {})
        joined = " ".join(argv)
        for flag in ("--die-with-parent", "--new-session", "--unshare-net", "--unshare-pid"):
            assert flag in argv
        assert "--ro-bind / /" in joined
        assert f"--tmpfs {tmp_path / 'home'}" in joined
        assert f"--ro-bind-try {tmp_path / 'repo'} {tmp_path / 'repo'}" in joined
        assert f"--bind-try {tmp_path / 'home' / '.entrabot'}" in joined
        assert f"--ro-bind /tmp/eb/p.sock {bwrap.PROXY_SOCKET_INSIDE}" in joined
        # home is hidden before anything in it is mounted back
        assert argv.index(str(tmp_path / "home")) < argv.index(str(tmp_path / "repo"))
        tail = argv[argv.index("--") + 1 :]
        assert tail[:3] == ["/venv/bin/python", "-m", "entrabot.sandbox.bridge"]
        assert tail[-3:] == ["/venv/bin/python", "-m", "entrabot.mcp_server"]

    def test_a_path_session_bus_is_mounted_and_an_abstract_one_is_not(self, tmp_path: Path) -> None:
        path_bus = self._command(
            tmp_path, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
        )
        assert "/run/user/1000/bus" in path_bus
        abstract = self._command(
            tmp_path, {"DBUS_SESSION_BUS_ADDRESS": "unix:abstract=/tmp/dbus-x"}
        )
        assert not any("dbus-x" in item for item in abstract)


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
