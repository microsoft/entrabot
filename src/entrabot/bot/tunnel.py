"""Dev Tunnel subprocess management for local bot development.

Wraps the ``devtunnel`` CLI to expose the bot's local aiohttp server
on a public HTTPS URL that Azure Bot Service can route to.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger("entrabot.bot.tunnel")

URL_PATTERN = re.compile(r"https://\S+\.devtunnels\.ms")


class TunnelError(Exception):
    """Raised when Dev Tunnel operations fail."""


class TunnelManager:
    """Manage a ``devtunnel host`` subprocess.

    Usage::

        mgr = TunnelManager(port=3978)
        url = mgr.start()     # returns public HTTPS URL
        # ... bot runs ...
        mgr.stop()
    """

    def __init__(self, port: int = 3978) -> None:
        self.port = port
        self._process: subprocess.Popen | None = None
        self._url: str | None = None

    @property
    def url(self) -> str | None:
        return self._url

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> str:
        """Start the Dev Tunnel and return the public HTTPS URL.

        Raises:
            TunnelError: If devtunnel CLI is not installed or tunnel fails to start.
        """
        try:
            self._process = subprocess.Popen(
                ["devtunnel", "host", "-p", str(self.port), "--allow-anonymous"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise TunnelError(
                "devtunnel CLI not found. Install: https://learn.microsoft.com/azure/developer/dev-tunnels/get-started"
            ) from None

        if self._process.poll() is not None:
            stderr = self._process.stderr.read().decode() if self._process.stderr else ""
            raise TunnelError(f"Dev Tunnel exited immediately: {stderr}")

        # Read stdout until we find the tunnel URL
        line = self._process.stdout.readline().decode() if self._process.stdout else ""
        match = URL_PATTERN.search(line)
        if match:
            self._url = match.group(0)
        else:
            self._url = f"https://localhost:{self.port}"

        logger.info("Dev Tunnel started: %s → localhost:%d", self._url, self.port)
        return self._url

    def stop(self) -> None:
        """Stop the Dev Tunnel subprocess."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            logger.info("Dev Tunnel stopped")
        self._process = None
        self._url = None
