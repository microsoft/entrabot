from __future__ import annotations

from collections.abc import Callable

import pytest

from entraclaw.a365.errors import A365TokenError
from entraclaw.a365.tokens import StaticA365TokenProvider, WorkIqTokenRequest


@pytest.mark.asyncio
async def test_static_token_provider_returns_token() -> None:
    provider = StaticA365TokenProvider("token-123")

    token = await provider.get_token(
        WorkIqTokenRequest(
            server_name="mcp_WordServer",
            audience="api://word-audience",
            scope="McpServers.Word.All",
        )
    )

    assert token == "token-123"


@pytest.mark.asyncio
async def test_static_token_provider_rejects_empty_token() -> None:
    provider = StaticA365TokenProvider("")

    with pytest.raises(A365TokenError) as exc_info:
        await provider.get_token(
            WorkIqTokenRequest(
                server_name="mcp_WordServer",
                audience="api://word-audience",
                scope="McpServers.Word.All",
            )
        )

    assert "api://word-audience" in str(exc_info.value)


@pytest.mark.asyncio
async def test_three_hop_provider_passes_audience_to_token_function() -> None:
    from entraclaw.a365.tokens import EntraclawA365TokenProvider

    calls: list[str] = []

    async def fake_acquire(audience: str) -> str:
        calls.append(audience)
        return "work-iq-token"

    provider = EntraclawA365TokenProvider(acquire_token=fake_acquire)
    token = await provider.get_token(
        WorkIqTokenRequest(
            server_name="mcp_WordServer",
            audience="api://word-audience",
            scope="McpServers.Word.All",
        )
    )

    assert token == "work-iq-token"
    assert calls == ["api://word-audience"]


@pytest.mark.asyncio
async def test_runtime_provider_runs_sync_three_hop_acquire_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from entraclaw.a365 import tokens
    from entraclaw.config import EntraClawConfig
    from entraclaw.tools import teams

    config = object()
    thread_calls: list[tuple[Callable[..., str], tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(EntraClawConfig, "from_env", classmethod(lambda cls: config))

    def fake_acquire(config_arg: object, *, resource_scope: str) -> str:
        assert config_arg is config
        assert resource_scope == "api://word-audience/.default"
        return "work-iq-token"

    async def fake_to_thread(
        func: Callable[..., str],
        /,
        *args: object,
        **kwargs: object,
    ) -> str:
        thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(teams, "acquire_agent_user_token", fake_acquire)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    token = await tokens._runtime_acquire_work_iq_token("api://word-audience")

    assert token == "work-iq-token"
    assert thread_calls == [
        (
            fake_acquire,
            (config,),
            {"resource_scope": "api://word-audience/.default"},
        )
    ]
