"""Cached storage-scope token provider for Azure Blob Storage (ADR-005).

``acquire_agent_user_storage_token`` performs a synchronous three-hop OAuth
exchange — three blocking HTTPS round-trips to Entra. Without caching,
every :class:`~entrabot.storage.blob.BlobStore` call re-runs that exchange.
At boot, watched-chat cursor rehydration does one blob read per watched
chat, so N watched chats meant N serialized three-hop exchanges executed
directly on the asyncio event loop — long enough to blow past the MCP
client's stdio handshake timeout (``-32001``).

:class:`StorageTokenCache` caches the token for
``STORAGE_TOKEN_REFRESH_THRESHOLD`` seconds and runs the blocking
acquisition via ``asyncio.to_thread`` so it can't starve the event loop
either. It's a module-level singleton because ``get_backend()`` and
``_get_conditional_store()`` each construct a fresh ``BlobStore`` per
call — the cache has to live above them to actually save round-trips.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from entrabot.config import get_config
from entrabot.tools.teams import acquire_agent_user_storage_token

# Mirrors mcp_server.TOKEN_REFRESH_THRESHOLD (55 min; 5-min buffer on the
# ~60-min Entra token lifetime).
STORAGE_TOKEN_REFRESH_THRESHOLD = 3300


class StorageTokenCache:
    """Caches the Agent User storage-scope token across BlobStore calls."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._acquired_at: float | None = None

    async def get(self) -> str:
        now = time.monotonic()
        if (
            self._token is None
            or self._acquired_at is None
            or (now - self._acquired_at) > STORAGE_TOKEN_REFRESH_THRESHOLD
        ):
            self._token = await asyncio.to_thread(acquire_agent_user_storage_token, get_config())
            self._acquired_at = now
        return self._token


_cache = StorageTokenCache()


def get_storage_token_provider() -> Callable[[], Awaitable[str]]:
    """Return the shared cache's ``get`` bound method for BlobStore DI."""
    return _cache.get
