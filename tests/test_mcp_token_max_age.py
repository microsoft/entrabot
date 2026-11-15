"""How long the server reuses a token: what bounds a disabled agent's remaining reach.

Entra keeps an issued access token valid until it expires, even after the agent is
disabled; Microsoft documents continuous access evaluation for users and single-tenant
workload identities, not for Agent Users. So the server asks for a fresh token every few
minutes (default 10), and when that sign-in fails every tool fails with it.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from entrabot import mcp_server
from entrabot.models import IdentityState


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 600.0), ("120", 120.0), ("5", 60.0), ("99999", 3300.0), ("soon", 600.0)],
)
def test_the_max_age_comes_from_the_environment_within_bounds(
    raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(mcp_server.TOKEN_MAX_AGE_ENV, raw)
    assert mcp_server._token_max_age() == expected


class FakeIdentity:
    def __init__(self, acquired_at: float) -> None:
        self.state = IdentityState.AGENT_USER
        self.session = SimpleNamespace(token_acquired_at=acquired_at)
        self.update_session = AsyncMock()


async def test_a_token_older_than_the_max_age_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(mcp_server.TOKEN_MAX_AGE_ENV, raising=False)
    identity = FakeIdentity(time.monotonic() - 601)
    asked: list[object] = []
    monkeypatch.setattr(mcp_server, "_identity", identity)
    monkeypatch.setattr(
        mcp_server, "acquire_agent_user_token", lambda config: asked.append(config) or "new"
    )
    await mcp_server._ensure_valid_token()
    assert asked and identity.update_session.await_count == 1


async def test_a_young_token_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mcp_server.TOKEN_MAX_AGE_ENV, raising=False)
    identity = FakeIdentity(time.monotonic() - 30)
    monkeypatch.setattr(mcp_server, "_identity", identity)
    monkeypatch.setattr(
        mcp_server, "acquire_agent_user_token", lambda config: pytest.fail("no refresh")
    )
    await mcp_server._ensure_valid_token()


async def test_a_disabled_agent_cannot_act_once_its_token_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot.errors import TokenExchangeError

    identity = FakeIdentity(time.monotonic() - 601)
    monkeypatch.setattr(mcp_server, "_identity", identity)

    def disabled(config: object) -> str:
        raise TokenExchangeError("hop2:agent_identity", "invalid_client", "AADSTS7000112: disabled")

    monkeypatch.setattr(mcp_server, "acquire_agent_user_token", disabled)
    with pytest.raises(TokenExchangeError):
        await mcp_server._ensure_valid_token()
    identity.update_session.assert_not_awaited()
