"""Tests for entrabot.storage.storage_token — cached storage-scope token.

Before this module existed, every BlobStore call re-ran the synchronous
three-hop OAuth exchange (acquire_agent_user_storage_token) uncached,
directly on the asyncio event loop. During boot, watched-chat cursor
rehydration does one blob read per watched chat, so N watched chats meant
N serialized, blocking three-hop exchanges — long enough to blow past the
MCP client's stdio handshake timeout (-32001).

StorageTokenCache.get() must:
- cache the token across calls within STORAGE_TOKEN_REFRESH_THRESHOLD
- re-acquire once the cached token is past the threshold
- run the blocking acquisition off the event loop (asyncio.to_thread)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from entrabot.storage.storage_token import (
    STORAGE_TOKEN_REFRESH_THRESHOLD,
    StorageTokenCache,
)


class TestStorageTokenCache:
    @pytest.mark.asyncio
    async def test_first_call_acquires_and_returns_token(self, monkeypatch) -> None:
        mock_acquire = MagicMock(return_value="fresh-storage-token")
        monkeypatch.setattr(
            "entrabot.storage.storage_token.acquire_agent_user_storage_token", mock_acquire
        )

        cache = StorageTokenCache()
        token = await cache.get()

        assert token == "fresh-storage-token"
        mock_acquire.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_call_within_threshold_reuses_cached_token(self, monkeypatch) -> None:
        mock_acquire = MagicMock(return_value="fresh-storage-token")
        monkeypatch.setattr(
            "entrabot.storage.storage_token.acquire_agent_user_storage_token", mock_acquire
        )

        cache = StorageTokenCache()
        first = await cache.get()
        second = await cache.get()

        assert first == second == "fresh-storage-token"
        mock_acquire.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_after_threshold_reacquires(self, monkeypatch) -> None:
        mock_acquire = MagicMock(side_effect=["token-1", "token-2"])
        monkeypatch.setattr(
            "entrabot.storage.storage_token.acquire_agent_user_storage_token", mock_acquire
        )

        cache = StorageTokenCache()
        first = await cache.get()
        assert first == "token-1"

        # Simulate the cached token aging past the refresh threshold.
        cache._acquired_at = time.monotonic() - (STORAGE_TOKEN_REFRESH_THRESHOLD + 1)
        second = await cache.get()

        assert second == "token-2"
        assert mock_acquire.call_count == 2

    @pytest.mark.asyncio
    async def test_acquisition_runs_off_event_loop(self, monkeypatch) -> None:
        """The blocking three-hop call must not starve the event loop."""

        def _blocking_acquire(config):
            time.sleep(0.2)
            return "fresh-storage-token"

        monkeypatch.setattr(
            "entrabot.storage.storage_token.acquire_agent_user_storage_token",
            _blocking_acquire,
        )

        cache = StorageTokenCache()

        async def _ticker() -> None:
            await asyncio.sleep(0.2)

        start = time.monotonic()
        await asyncio.gather(cache.get(), _ticker())
        elapsed = time.monotonic() - start

        assert elapsed < 0.35, (
            f"event loop was starved by the blocking token acquisition (took {elapsed:.3f}s)"
        )
