"""One Graph token acquisition per provider, even during concurrent polling/tool calls."""

import asyncio
from unittest.mock import AsyncMock

import jwt
import pytest

from entrabot.config import EntraBotConfig
from entrabot.harness.teams import auth


@pytest.mark.parametrize("expired", [False, True])
async def test_concurrent_cache_misses_share_one_token_acquisition(monkeypatch, expired):
    config = EntraBotConfig(
        tenant_id="fixture-tenant", blueprint_app_id="fixture-blueprint",
        blueprint_cert_thumbprint="fixture-thumb", agent_id="fixture-agent",
        agent_user_id="fixture-user",
    )
    monkeypatch.setattr("entrabot.config.get_config", lambda: config)
    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    token = jwt.encode({"exp": 5000}, "fixture-key-with-at-least-32-bytes", algorithm="HS256")

    async def acquire(*args):
        await asyncio.sleep(0)
        return token

    acquire_mock = AsyncMock(side_effect=acquire)
    monkeypatch.setattr(auth.asyncio, "to_thread", acquire_mock)
    provider = auth._three_hop_provider()
    assert provider is not None
    if expired:
        assert await provider() == token
        acquire_mock.reset_mock()
        monkeypatch.setattr(auth.time, "time", lambda: 4900)
        token = jwt.encode({"exp": 9000}, "fixture-key-with-at-least-32-bytes", algorithm="HS256")

    assert await asyncio.gather(provider(), provider(), provider()) == [token] * 3
    acquire_mock.assert_awaited_once()
