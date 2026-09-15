"""In-memory Agent 365 observability token cache and refresh boundary."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock

import httpx
from keyring.errors import KeyringError

from entrabot.auth.cncrypt_signer import SigningError
from entrabot.config import EntraBotConfig
from entrabot.errors import AuthError, ConfigError, InsecureKeyringBackendError

OBSERVABILITY_SCOPE = (
    "api://9b975845-388f-4429-889e-eab1ef63949c/Agent365.Observability.OtelWrite"
)
REFRESH_SKEW_SECONDS = 300
REFRESH_LEAD_SECONDS = 60
REFRESH_RETRY_SECONDS = 5
MAX_REFRESH_RETRY_SECONDS = 30


@dataclass(frozen=True)
class CachedObservabilityToken:
    """An observability token and its absolute expiry time."""

    token: str = field(repr=False)
    expires_at: float


class ObservabilityTokenCache:
    """Thread-safe cache keyed by the exact Agent Identity and tenant IDs."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = RLock()
        self._tokens: dict[tuple[str, str], CachedObservabilityToken] = {}

    def store(
        self,
        agent_id: str,
        tenant_id: str,
        token: str,
        *,
        expires_at: float,
    ) -> None:
        """Store a token for one exact agent and tenant pair."""
        cached = CachedObservabilityToken(token=token, expires_at=expires_at)
        with self._lock:
            self._tokens[(agent_id, tenant_id)] = cached

    def resolve(self, agent_id: str, tenant_id: str) -> str | None:
        """Return a usable token without performing acquisition or other I/O."""
        if not agent_id.strip() or not tenant_id.strip():
            return None

        key = (agent_id, tenant_id)
        with self._lock:
            cached = self._tokens.get(key)
            if cached is None:
                return None
            if cached.expires_at <= self._now() + REFRESH_SKEW_SECONDS:
                del self._tokens[key]
                return None
            return cached.token

    def clear(self) -> None:
        """Remove all cached observability tokens."""
        with self._lock:
            self._tokens.clear()

    def refresh_delay(self, agent_id: str, tenant_id: str) -> float:
        """Schedule renewal before the resolver's cutoff, without evicting the token."""
        with self._lock:
            cached = self._tokens.get((agent_id, tenant_id))
            if cached is None:
                return 0.0
            usable_seconds = cached.expires_at - self._now() - REFRESH_SKEW_SECONDS
        # Short-lived tokens renew halfway through their remaining usable window.
        lead = min(REFRESH_LEAD_SECONDS, usable_seconds / 2)
        return max(0.0, usable_seconds - lead)


_TOKEN_CACHE = ObservabilityTokenCache()


def resolve_observability_token(agent_id: str, tenant_id: str) -> str | None:
    """Synchronously resolve a cached token for Microsoft OpenTelemetry."""
    return _TOKEN_CACHE.resolve(agent_id, tenant_id)


def _decode_expiry(token: str, *, now: float) -> float:
    if not token or not token.strip():
        raise ValueError("Observability token is empty")

    import jwt

    try:
        claims = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
        )
    except jwt.PyJWTError as exc:
        raise ValueError("Observability token is a malformed JWT") from exc

    expires_at = claims.get("exp")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("Observability token must contain a numeric exp claim")

    expiry = float(expires_at)
    if not math.isfinite(expiry):
        raise ValueError("Observability token must contain a finite numeric exp claim")
    if expiry <= now:
        raise ValueError("Observability token is already expired")
    return expiry


def _identity_key(config: EntraBotConfig) -> tuple[str, str]:
    missing = [
        name
        for name, value in (("agent_id", config.agent_id), ("tenant_id", config.tenant_id))
        if value is None or not value.strip()
    ]
    if missing:
        raise ValueError(
            f"Missing required observability config fields: {', '.join(missing)}"
        )

    agent_id = config.agent_id
    tenant_id = config.tenant_id
    assert agent_id is not None
    assert tenant_id is not None
    return agent_id, tenant_id


async def refresh_observability_token(
    config: EntraBotConfig,
    *,
    force: bool = False,
) -> None:
    """Acquire and cache a fresh delegated A365 observability token."""
    if not config.a365_observability_enabled or not config.a365_export_enabled:
        return

    agent_id, tenant_id = _identity_key(config)
    if not force and _TOKEN_CACHE.resolve(agent_id, tenant_id) is not None:
        return

    from entrabot.tools.teams import acquire_agent_user_token

    # Cancellation cannot interrupt synchronous HTTP, but the worker only returns a token.
    # Keeping the cache write here discards late results after the owner is disposed.
    token = await asyncio.to_thread(
        acquire_agent_user_token,
        config,
        resource_scope=OBSERVABILITY_SCOPE,
    )
    now = time.time()
    expires_at = _decode_expiry(token, now=now)
    if expires_at <= now + REFRESH_SKEW_SECONDS:
        raise ValueError("Observability token is too near expiry to be cached")
    _TOKEN_CACHE.store(
        agent_id,
        tenant_id,
        token,
        expires_at=expires_at,
    )


async def run_observability_token_refresh(
    config: EntraBotConfig,
    *,
    on_error: Callable[[Exception], None],
) -> None:
    """Keep the synchronous resolver populated until the session cancels this task."""
    if not config.a365_observability_enabled or not config.a365_export_enabled:
        return

    agent_id, tenant_id = _identity_key(config)
    delay = _TOKEN_CACHE.refresh_delay(agent_id, tenant_id)
    retry_delay = REFRESH_RETRY_SECONDS
    while True:
        await asyncio.sleep(delay)
        try:
            await refresh_observability_token(config, force=True)
        except (
            AuthError, ConfigError, InsecureKeyringBackendError, KeyringError,
            SigningError, httpx.HTTPError, OSError, ValueError,
        ) as error:
            # Auth exceptions may contain raw response bodies or credential material.
            on_error(AuthError(
                f"Token refresh failed ({type(error).__name__}); "
                f"retrying in {retry_delay} seconds. Check credentials and A365 authorization."
            ))
            delay = retry_delay
            retry_delay = min(retry_delay * 2, MAX_REFRESH_RETRY_SECONDS)
        else:
            retry_delay = REFRESH_RETRY_SECONDS
            delay = _TOKEN_CACHE.refresh_delay(agent_id, tenant_id)
