from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from threading import Event
from types import SimpleNamespace

import httpx
import jwt
import pytest
from keyring.errors import KeyringError

from entrabot.auth.cncrypt_signer import SigningError
from entrabot.config import EntraBotConfig
from entrabot.errors import (
    AgentIDNotAvailable,
    ConfigError,
    InsecureKeyringBackendError,
    TokenExchangeError,
)
from entrabot.observability import tokens as tokens_module


def make_jwt(expiry: object) -> str:
    return jwt.encode({"exp": expiry}, "test-signing-key", algorithm="HS256")


def enabled_config(**overrides: object) -> EntraBotConfig:
    values: dict[str, object] = {
        "a365_observability_enabled": True,
        "a365_export_enabled": True,
        "agent_id": "agent-id",
        "tenant_id": "tenant-id",
    }
    values.update(overrides)
    return EntraBotConfig(**values)


def test_cache_isolates_exact_agent_and_tenant_keys() -> None:
    cache = tokens_module.ObservabilityTokenCache(now=lambda: 1_000.0)
    cache.store("agent-a", "tenant-a", "token-a", expires_at=2_000.0)
    cache.store("agent-b", "tenant-a", "token-b", expires_at=2_000.0)
    cache.store("agent-a", "tenant-b", "token-c", expires_at=2_000.0)

    assert cache.resolve("agent-a", "tenant-a") == "token-a"
    assert cache.resolve("agent-b", "tenant-a") == "token-b"
    assert cache.resolve("agent-a", "tenant-b") == "token-c"
    assert cache.resolve("agent-b", "tenant-b") is None
    assert cache.resolve("", "tenant-a") is None
    assert cache.resolve("agent-a", " ") is None


@pytest.mark.parametrize("expires_at", [999.0, 1_000.0, 1_299.0, 1_300.0])
def test_cache_removes_expired_or_near_expiry_entries(expires_at: float) -> None:
    cache = tokens_module.ObservabilityTokenCache(now=lambda: 1_000.0)
    cache.store("agent-id", "tenant-id", "distinctive-secret", expires_at=expires_at)

    assert cache.resolve("agent-id", "tenant-id") is None
    assert cache._tokens == {}


def test_cached_token_repr_and_str_do_not_leak_token() -> None:
    secret = "distinctive-secret-token"
    cached = tokens_module.CachedObservabilityToken(
        token=secret,
        expires_at=2_000.0,
    )

    assert secret not in repr(cached)
    assert secret not in str(cached)


def test_sync_resolver_only_reads_global_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class ReadOnlyCache:
        def resolve(self, agent_id: str, tenant_id: str) -> str | None:
            calls.append((agent_id, tenant_id))
            return "cached-token"

    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", ReadOnlyCache())

    assert (
        tokens_module.resolve_observability_token("agent-id", "tenant-id")
        == "cached-token"
    )
    assert calls == [("agent-id", "tenant-id")]


@pytest.mark.parametrize(
    ("observability_enabled", "export_enabled"),
    [(False, False), (False, True), (True, False)],
)
async def test_refresher_is_noop_when_observability_or_export_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    observability_enabled: bool,
    export_enabled: bool,
) -> None:
    async def unexpected_to_thread(*args: object, **kwargs: object) -> str:
        pytest.fail("Disabled observability must not acquire a token")

    monkeypatch.setattr(asyncio, "to_thread", unexpected_to_thread)

    await tokens_module.refresh_observability_token(
        EntraBotConfig(
            a365_observability_enabled=observability_enabled,
            a365_export_enabled=export_enabled,
        )
    )


@pytest.mark.parametrize(
    ("overrides", "missing_field"),
    [
        ({"agent_id": None}, "agent_id"),
        ({"agent_id": " "}, "agent_id"),
        ({"tenant_id": None}, "tenant_id"),
        ({"tenant_id": ""}, "tenant_id"),
    ],
)
async def test_refresher_names_missing_identity_fields(
    overrides: dict[str, object],
    missing_field: str,
) -> None:
    with pytest.raises(ValueError, match=missing_field):
        await tokens_module.refresh_observability_token(enabled_config(**overrides))


async def test_refresher_acquires_in_thread_with_exact_scope_and_caches_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot.tools import teams

    now = time.time()
    token = make_jwt(now + 3_600.0)
    cache = tokens_module.ObservabilityTokenCache(now=lambda: now)
    thread_calls: list[
        tuple[Callable[..., str], tuple[object, ...], dict[str, object]]
    ] = []

    def fake_acquire(config: EntraBotConfig, *, resource_scope: str) -> str:
        assert config == enabled_config()
        assert resource_scope == tokens_module.OBSERVABILITY_SCOPE
        return token

    async def fake_to_thread(
        func: Callable[..., str],
        /,
        *args: object,
        **kwargs: object,
    ) -> str:
        thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", cache)
    monkeypatch.setattr(teams, "acquire_agent_user_token", fake_acquire)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    await tokens_module.refresh_observability_token(enabled_config())

    assert thread_calls == [
        (
            fake_acquire,
            (enabled_config(),),
            {"resource_scope": tokens_module.OBSERVABILITY_SCOPE},
        )
    ]
    assert tokens_module.resolve_observability_token("agent-id", "tenant-id") == token


@pytest.mark.parametrize(
    ("token_factory", "error_match"),
    [
        (lambda now: "", "empty"),
        (lambda now: "not-a-jwt", "malformed"),
        (lambda now: jwt.encode({}, "key", algorithm="HS256"), "exp"),
        (lambda now: make_jwt("not-a-number"), "numeric"),
        (lambda now: make_jwt(now), "expired"),
        (lambda now: make_jwt(now - 1.0), "expired"),
    ],
    ids=["empty", "malformed", "missing-exp", "non-numeric-exp", "now", "expired"],
)
async def test_invalid_acquired_token_raises_without_caching(
    monkeypatch: pytest.MonkeyPatch,
    token_factory: Callable[[float], str],
    error_match: str,
) -> None:
    from entrabot.tools import teams

    now = time.time()
    cache = tokens_module.ObservabilityTokenCache(now=lambda: now)

    def fake_acquire(config: EntraBotConfig, *, resource_scope: str) -> str:
        return token_factory(now)

    async def fake_to_thread(
        func: Callable[..., str],
        /,
        *args: object,
        **kwargs: object,
    ) -> str:
        return func(*args, **kwargs)

    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", cache)
    monkeypatch.setattr(teams, "acquire_agent_user_token", fake_acquire)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    with pytest.raises(ValueError, match=error_match):
        await tokens_module.refresh_observability_token(enabled_config())

    assert tokens_module.resolve_observability_token("agent-id", "tenant-id") is None


async def test_force_controls_reacquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    from entrabot.tools import teams

    now = time.time()
    original_token = make_jwt(now + 3_600.0)
    refreshed_token = make_jwt(now + 7_200.0)
    cache = tokens_module.ObservabilityTokenCache(now=lambda: now)
    cache.store(
        "agent-id",
        "tenant-id",
        original_token,
        expires_at=now + 3_600.0,
    )
    acquisitions = 0

    def fake_acquire(config: EntraBotConfig, *, resource_scope: str) -> str:
        nonlocal acquisitions
        acquisitions += 1
        return refreshed_token

    async def fake_to_thread(
        func: Callable[..., str],
        /,
        *args: object,
        **kwargs: object,
    ) -> str:
        return func(*args, **kwargs)

    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", cache)
    monkeypatch.setattr(teams, "acquire_agent_user_token", fake_acquire)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    await tokens_module.refresh_observability_token(enabled_config(), force=False)

    assert acquisitions == 0
    assert (
        tokens_module.resolve_observability_token("agent-id", "tenant-id")
        == original_token
    )

    await tokens_module.refresh_observability_token(enabled_config(), force=True)

    assert acquisitions == 1
    assert (
        tokens_module.resolve_observability_token("agent-id", "tenant-id")
        == refreshed_token
    )


@pytest.mark.parametrize(
    ("lifetime", "expected_delay"),
    [(3_600, 3_240), (900, 540), (320, 10), (300, 0), (299, 0)],
)
def test_refresh_delay_precedes_skew_cutoff(lifetime: float, expected_delay: float) -> None:
    cache = tokens_module.ObservabilityTokenCache(now=lambda: 1_000.0)
    cache.store("agent-id", "tenant-id", "token", expires_at=1_000 + lifetime)

    assert cache.refresh_delay("agent-id", "tenant-id") == expected_delay
    assert cache.refresh_delay("another-agent", "tenant-id") == 0
    assert cache.refresh_delay("agent-id", "another-tenant") == 0


async def test_background_refresh_keeps_resolver_available_across_token_lifetimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot.tools import teams

    now = 1_000.0
    issued: list[str] = []
    delays: list[float] = []
    config = enabled_config(agent_id=" Agent-ID ", tenant_id=" Tenant-ID ")
    cache = tokens_module.ObservabilityTokenCache(now=lambda: now)

    async def sleep(delay: float) -> None:
        nonlocal now
        assert cache.resolve(" Agent-ID ", " Tenant-ID ") == issued[-1]
        assert cache.resolve("Agent-ID", "Tenant-ID") is None
        if len(issued) == 5:
            raise asyncio.CancelledError
        delays.append(delay)
        now += delay
        assert cache.resolve(" Agent-ID ", " Tenant-ID ") == issued[-1]

    def acquire(received_config: EntraBotConfig, *, resource_scope: str) -> str:
        nonlocal now
        assert received_config is config
        assert resource_scope == tokens_module.OBSERVABILITY_SCOPE
        if issued:
            now += 40  # A slow three-hop exchange still completes before the cutoff.
            assert cache.resolve(" Agent-ID ", " Tenant-ID ") == issued[-1]
        token = make_jwt(now + 900)
        issued.append(token)
        return token

    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", cache)
    monkeypatch.setattr(tokens_module, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(
        tokens_module, "asyncio", SimpleNamespace(sleep=sleep, to_thread=asyncio.to_thread)
    )
    monkeypatch.setattr(teams, "acquire_agent_user_token", acquire)
    await tokens_module.refresh_observability_token(config)
    errors: list[Exception] = []

    with pytest.raises(asyncio.CancelledError):
        await tokens_module.run_observability_token_refresh(config, on_error=errors.append)

    assert len(issued) == 5
    assert delays == [540] * 4
    assert now > 1_000 + 2 * 900
    assert errors == []
    assert cache.resolve(" Agent-ID ", " Tenant-ID ") == issued[-1]


@pytest.mark.parametrize(
    "error",
    [
        TokenExchangeError("hop3", "secret-error", "secret-response"),
        AgentIDNotAvailable("secret-response"),
        ConfigError("secret-response"),
        httpx.ReadTimeout("secret-response"),
        ValueError("secret-response"),
        KeyringError("secret-response"),
        SigningError("secret-response"),
        OSError("secret-response"),
        InsecureKeyringBackendError("secret-response"),
    ],
)
async def test_expected_background_failure_is_sanitized_and_retried(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    now = 1_000.0
    cache = tokens_module.ObservabilityTokenCache(now=lambda: now)
    cache.store("agent-id", "tenant-id", "original", expires_at=4_600)
    errors: list[Exception] = []
    delays: list[float] = []
    attempts = 0

    async def sleep(delay: float) -> None:
        nonlocal now
        if attempts == 7:
            raise asyncio.CancelledError
        delays.append(delay)
        now += delay

    async def refresh(config: EntraBotConfig, *, force: bool = False) -> None:
        nonlocal attempts
        attempts += 1
        assert force is True
        if attempts <= 6:
            raise error
        cache.store("agent-id", "tenant-id", "renewed", expires_at=now + 3_600)

    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", cache)
    monkeypatch.setattr(tokens_module, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(tokens_module, "refresh_observability_token", refresh)

    with pytest.raises(asyncio.CancelledError):
        await tokens_module.run_observability_token_refresh(
            enabled_config(), on_error=errors.append
        )

    assert delays == [3_240, 5, 10, 20, 30, 30, 30]
    assert len(errors) == 6
    assert all(type(error).__name__ in str(warning) for warning in errors)
    assert all("retry" in str(warning).lower() for warning in errors)
    assert all("secret" not in repr(warning) for warning in errors)
    assert all(warning.__cause__ is None and warning.__context__ is None for warning in errors)
    assert cache.resolve("agent-id", "tenant-id") == "renewed"


@pytest.mark.parametrize(
    ("observability_enabled", "export_enabled"),
    [(False, False), (False, True), (True, False)],
)
async def test_background_loop_is_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch, observability_enabled: bool, export_enabled: bool,
) -> None:
    async def unexpected_refresh(*args: object, **kwargs: object) -> None:
        pytest.fail("Disabled observability must not refresh")

    monkeypatch.setattr(tokens_module, "refresh_observability_token", unexpected_refresh)
    await tokens_module.run_observability_token_refresh(
        EntraBotConfig(
            a365_observability_enabled=observability_enabled,
            a365_export_enabled=export_enabled,
        ),
        on_error=lambda error: pytest.fail(str(error)),
    )


async def test_background_programming_error_is_not_silently_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_refresh(*args: object, **kwargs: object) -> None:
        raise TypeError("programming error")

    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", tokens_module.ObservabilityTokenCache())
    monkeypatch.setattr(tokens_module, "refresh_observability_token", broken_refresh)
    with pytest.raises(TypeError, match="programming error"):
        await tokens_module.run_observability_token_refresh(
            enabled_config(), on_error=lambda error: pytest.fail(str(error))
        )


async def test_cancelled_thread_acquisition_never_repopulates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot.tools import teams

    loop = asyncio.get_running_loop()
    prior_tasks = asyncio.all_tasks()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = Event()
    cache = tokens_module.ObservabilityTokenCache()

    def acquire(*args: object, **kwargs: object) -> str:
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(timeout=5):
                raise TimeoutError("test acquisition was not released")
            return make_jwt(time.time() + 3_600)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", cache)
    monkeypatch.setattr(teams, "acquire_agent_user_token", acquire)
    task = asyncio.create_task(tokens_module.run_observability_token_refresh(
        enabled_config(), on_error=lambda error: pytest.fail(str(error))
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)

    await asyncio.sleep(0)
    assert cache.resolve("agent-id", "tenant-id") is None
    assert asyncio.all_tasks() == prior_tasks


async def test_near_expiry_acquisition_fails_without_replacing_usable_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot.tools import teams

    now = time.time()
    cache = tokens_module.ObservabilityTokenCache(now=lambda: now)
    cache.store("agent-id", "tenant-id", "original", expires_at=now + 3_600)
    monkeypatch.setattr(tokens_module, "_TOKEN_CACHE", cache)
    monkeypatch.setattr(
        teams, "acquire_agent_user_token", lambda *args, **kwargs: make_jwt(now + 299)
    )
    with pytest.raises(ValueError, match="expiry"):
        await tokens_module.refresh_observability_token(enabled_config(), force=True)

    assert cache.resolve("agent-id", "tenant-id") == "original"
