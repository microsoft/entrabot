"""The allowlisting CONNECT proxy: the contained server's only way out."""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest

from entrabot.sandbox.proxy import AllowlistProxy


@pytest.fixture
def echo_port() -> int:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            data = conn.recv(1024)
            conn.sendall(b"echo:" + data)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


def _ask(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = sock.recv(1024)
            if not chunk:
                break
            reply += chunk
        if reply.startswith(b"HTTP/1.1 200"):
            sock.sendall(b"ping")
            reply += sock.recv(1024)
        return reply


def test_an_allowed_host_and_port_gets_a_tunnel(echo_port: int) -> None:
    proxy = AllowlistProxy(["127.0.0.1"], allowed_ports=[echo_port])
    port = proxy.start_tcp()
    try:
        reply = _ask(port, f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    finally:
        proxy.stop()
    assert reply.startswith(b"HTTP/1.1 200") and reply.endswith(b"echo:ping")
    assert proxy.refused == []


def test_a_host_not_on_the_list_is_refused_and_recorded(echo_port: int) -> None:
    seen: list[str] = []
    proxy = AllowlistProxy(["graph.microsoft.com"], allowed_ports=[443], on_refusal=seen.append)
    port = proxy.start_tcp()
    try:
        reply = _ask(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    finally:
        proxy.stop()
    assert reply.startswith(b"HTTP/1.1 403")
    assert proxy.refused == ["example.com:443"] and seen == ["example.com:443"]


def test_an_allowed_host_on_another_port_is_refused(echo_port: int) -> None:
    proxy = AllowlistProxy(["127.0.0.1"], allowed_ports=[443])
    port = proxy.start_tcp()
    try:
        reply = _ask(port, f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n\r\n".encode())
    finally:
        proxy.stop()
    assert reply.startswith(b"HTTP/1.1 403")


def test_plain_http_is_refused() -> None:
    proxy = AllowlistProxy(["graph.microsoft.com"])
    port = proxy.start_tcp()
    try:
        reply = _ask(port, b"GET http://graph.microsoft.com/ HTTP/1.1\r\n\r\n")
    finally:
        proxy.stop()
    assert reply.startswith(b"HTTP/1.1 405")


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket")
def test_the_unix_socket_is_private_to_this_user() -> None:
    import tempfile

    proxy = AllowlistProxy(["graph.microsoft.com"])
    # Short: a Unix socket path is limited to about 100 bytes.
    path = Path(tempfile.mkdtemp(prefix="eb", dir="/tmp")) / "p.sock"
    proxy.start_unix(path)
    try:
        assert (path.stat().st_mode & 0o077) == 0
    finally:
        proxy.stop()
