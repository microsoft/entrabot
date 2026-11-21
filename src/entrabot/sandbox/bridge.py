"""Inside the Linux network namespace: a loopback port forwarded to the launcher's proxy.

bubblewrap gives the server a network namespace with nothing but loopback. This runs first
in it: it listens on ``127.0.0.1:<port>``, forwards every connection to the proxy's Unix
socket (mounted in), starts the server with the same standard input and output, and exits
with the server's exit code.

``python -m entrabot.sandbox.bridge --unix <socket> --port <port> -- <server argv>``
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading

from entrabot.sandbox.proxy import relay


def serve(listener: socket.socket, unix_path: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(unix_path)
        except OSError:
            client.close()
            continue
        threading.Thread(target=relay, args=(client, upstream), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--unix", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command to run")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(64)
    threading.Thread(target=serve, args=(listener, args.unix), daemon=True).start()
    return subprocess.call(command)


if __name__ == "__main__":
    sys.exit(main())
