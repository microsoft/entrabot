"""The contained server's only way out: an HTTPS CONNECT proxy with a host allowlist.

The sandbox lets the server connect to nothing but this proxy (a loopback port on macOS, a
Unix socket bridged into the network namespace on Linux). The proxy runs in the launcher,
outside the sandbox, and opens a tunnel only to an allowed host on an allowed port (443).
Plain HTTP, other methods, other hosts and other ports are refused, and each refusal is
recorded. TLS stays end to end: the proxy sees host names, never content.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from entrabot.sandbox.policy import host_allowed

log = logging.getLogger(__name__)

_MAX_HEADER = 8192
_CONNECT_TIMEOUT = 15.0


class AllowlistProxy:
    def __init__(
        self,
        allowed_hosts: Sequence[str],
        *,
        allowed_ports: Sequence[int] = (443,),
        on_refusal: Callable[[str], None] | None = None,
        connect: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        self.allowed_hosts = tuple(allowed_hosts)
        self.allowed_ports = tuple(allowed_ports)
        self.refused: list[str] = []
        self._on_refusal = on_refusal
        self._connect = connect
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()

    # ── listening ────────────────────────────────────────────────────────────

    def start_tcp(self, host: str = "127.0.0.1") -> int:
        """Listen on a free loopback port; returns it."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, 0))
        self._serve(listener)
        return listener.getsockname()[1]

    def start_unix(self, path: Path) -> None:
        """Listen on a Unix socket only this user can open."""
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(old)
        self._serve(listener)

    def _serve(self, listener: socket.socket) -> None:
        listener.listen(64)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="proxy")
        self._thread.start()

    def stop(self) -> None:
        self._closing.set()
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._listener.close()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._closing.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    # ── one connection ───────────────────────────────────────────────────────

    def _refuse(self, client: socket.socket, status: str, what: str) -> None:
        self.refused.append(what)
        log.warning("containment proxy refused %s", what)
        if self._on_refusal:
            with contextlib.suppress(Exception):
                self._on_refusal(what)
        with contextlib.suppress(OSError):
            client.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
            client.close()

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(_CONNECT_TIMEOUT)
        head = b""
        try:
            while b"\r\n\r\n" not in head and len(head) < _MAX_HEADER:
                chunk = client.recv(4096)
                if not chunk:
                    client.close()
                    return
                head += chunk
        except OSError:
            client.close()
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            self._refuse(client, "405 Method Not Allowed", f"non-CONNECT request {parts[:1]}")
            return
        target = parts[1]
        host, _, port_text = target.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_text)
        except ValueError:
            self._refuse(client, "400 Bad Request", f"bad target {target!r}")
            return
        if port not in self.allowed_ports or not host_allowed(host, self.allowed_hosts):
            self._refuse(client, "403 Forbidden", f"{host}:{port}")
            return
        try:
            upstream = self._connect((host, port), timeout=_CONNECT_TIMEOUT)
        except OSError as exc:
            log.warning("containment proxy could not reach %s:%s: %s", host, port, exc)
            with contextlib.suppress(OSError):
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                client.close()
            return
        leftover = head.split(b"\r\n\r\n", 1)[1]
        client.settimeout(None)
        upstream.settimeout(None)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if leftover:
            upstream.sendall(leftover)
        relay(client, upstream)


def relay(a: socket.socket, b: socket.socket) -> None:
    """Copy bytes both ways until either side closes."""

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

    other = threading.Thread(target=pump, args=(b, a), daemon=True)
    other.start()
    pump(a, b)
    other.join()
    for sock in (a, b):
        with contextlib.suppress(OSError):
            sock.close()
