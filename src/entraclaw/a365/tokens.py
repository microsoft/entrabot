"""Token acquisition boundary for Agent 365 Work IQ MCP servers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from entraclaw.a365.errors import A365TokenError


@dataclass(frozen=True)
class WorkIqTokenRequest:
    """Token request for one Work IQ MCP server audience."""

    server_name: str
    audience: str
    scope: str


class A365TokenProvider(Protocol):
    """Protocol implemented by Work IQ token providers."""

    async def get_token(self, request: WorkIqTokenRequest) -> str:
        """Return a bearer token for the requested Work IQ audience."""


@dataclass(frozen=True)
class StaticA365TokenProvider:
    """Test token provider."""

    token: str

    async def get_token(self, request: WorkIqTokenRequest) -> str:
        if not self.token:
            raise A365TokenError(request.audience, "static token is empty")
        return self.token


AcquireTokenFunc = Callable[[str], Awaitable[str]]


async def _runtime_acquire_work_iq_token(audience: str) -> str:
    """Acquire a Work IQ token using Entraclaw runtime auth.

    The first implementation delegates to the existing three-hop code after
    Task 0 confirms the audience shape. Keep the import local so unit tests can
    use the injected function without booting runtime config.
    """
    from entraclaw.config import EntraClawConfig
    from entraclaw.tools.teams import acquire_agent_user_token

    config = EntraClawConfig.from_env()
    scope = audience if audience.endswith("/.default") else f"{audience}/.default"
    return await asyncio.to_thread(acquire_agent_user_token, config, resource_scope=scope)


@dataclass(frozen=True)
class EntraclawA365TokenProvider:
    """Entraclaw-backed Work IQ token provider."""

    acquire_token: AcquireTokenFunc = _runtime_acquire_work_iq_token

    async def get_token(self, request: WorkIqTokenRequest) -> str:
        try:
            token = await self.acquire_token(request.audience)
        except Exception as exc:
            raise A365TokenError(request.audience, str(exc)) from exc
        if not token:
            raise A365TokenError(request.audience, "token provider returned empty token")
        return token
