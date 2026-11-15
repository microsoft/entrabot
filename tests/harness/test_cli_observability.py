from __future__ import annotations

import builtins
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from entrabot.config import EntraBotConfig
from entrabot.harness import cli
from entrabot.harness.cli import subcommands
from entrabot.harness.config import HarnessConfig


def configured_harness() -> HarnessConfig:
    return HarnessConfig(
        name="test-agent",
        description="Test agent",
        agent_id="harness-agent-id",
        created_utc="2026-08-28T00:00:00+00:00",
    )


@pytest.mark.parametrize("error", [PermissionError("unreadable overlay"), ValueError("conflict")])
@pytest.mark.parametrize("command", ["run", "doctor"])
async def test_identity_resolution_failure_stops_before_auth_or_telemetry(
    monkeypatch, tmp_path, error, command,
):
    from entrabot import config as config_module
    from entrabot.harness import session as session_module
    from entrabot.harness import teams as teams_module
    from entrabot.observability import runtime, tokens

    monkeypatch.setattr(config_module, "apply_agent_env", Mock(side_effect=error))
    monkeypatch.setattr(subcommands.cfgmod, "try_load", lambda root: configured_harness())
    initialize = Mock(side_effect=AssertionError("must not initialize telemetry"))
    provider = Mock(side_effect=AssertionError("must not create token provider"))
    session = Mock(side_effect=AssertionError("must not start session"))
    refresh = AsyncMock()
    monkeypatch.setattr(runtime, "initialize_observability", initialize)
    monkeypatch.setattr(teams_module, "make_token_provider", provider)
    monkeypatch.setattr(session_module, "InteractiveSession", session)
    monkeypatch.setattr(tokens, "refresh_observability_token", refresh)

    with pytest.raises(type(error), match=str(error)):
        if command == "run":
            await cli._cmd_run(set(), str(tmp_path))
        else:
            await cli._cmd_doctor(str(tmp_path))
    initialize.assert_not_called()
    refresh.assert_not_called()
    provider.assert_not_called()
    session.assert_not_called()


@pytest.mark.parametrize("scaffold", [False, True])
async def test_run_refreshes_observability_before_session_startup(
    monkeypatch: pytest.MonkeyPatch,
    scaffold: bool,
) -> None:
    from entrabot import config as config_module
    from entrabot.harness import session as session_module
    from entrabot.harness import teams as teams_module
    from entrabot.observability import runtime, tokens

    events: list[str] = []
    observability_config = EntraBotConfig(
        a365_observability_enabled=True,
        a365_export_enabled=True,
    )
    initialized_with: list[EntraBotConfig] = []
    refreshed_with: list[EntraBotConfig] = []

    def apply_identity(root: str) -> None:
        assert root == "agent-root"
        events.append("apply_identity")

    def get_config() -> EntraBotConfig:
        events.append("get_config")
        return observability_config

    saved: list[HarnessConfig] = []

    def load(root: str) -> HarnessConfig | None:
        events.append("load_harness")
        return None if scaffold else configured_harness()

    def save(root: str, config: HarnessConfig) -> None:
        events.append("save_harness")
        saved.append(config)

    def initialize(config: EntraBotConfig, *, agent_name: str) -> None:
        events.append("initialize")
        initialized_with.append(config)
        assert agent_name == ("agent-root-agent" if scaffold else "test-agent")
        assert "load_harness" in events
        if scaffold:
            assert saved[0].name == agent_name
            assert saved[0].agent_id

    async def refresh(config: EntraBotConfig) -> None:
        events.append("refresh")
        refreshed_with.append(config)

    def make_token_provider() -> object:
        events.append("token_provider")
        return object()

    class FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            events.append("session_construct")

        async def run(self) -> None:
            events.append("session_run")

    monkeypatch.setattr(subcommands, "_apply_agent_identity", apply_identity)
    monkeypatch.setattr(config_module, "get_config", get_config)
    monkeypatch.setattr(runtime, "initialize_observability", initialize)
    monkeypatch.setattr(tokens, "refresh_observability_token", refresh)
    monkeypatch.setattr(subcommands.cfgmod, "try_load", load)
    monkeypatch.setattr(subcommands.cfgmod, "save", save)
    monkeypatch.setattr(subcommands, "_pick_ui", object)
    monkeypatch.setattr(teams_module, "make_token_provider", make_token_provider)
    monkeypatch.setattr(session_module, "InteractiveSession", FakeSession)
    original_import = builtins.__import__
    session_imports: list[str] = []

    def import_after_instrumentation(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 2 and name in ("session", "teams"):
            assert events.index("initialize") < events.index("refresh")
            session_imports.append(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_after_instrumentation)

    assert await cli._cmd_run(set(), "agent-root") == 0

    assert events == [
        "apply_identity",
        "load_harness",
        *(["save_harness"] if scaffold else []),
        "get_config",
        "initialize",
        "refresh",
        "token_provider",
        "session_construct",
        "session_run",
    ]
    assert initialized_with == [observability_config]
    assert refreshed_with == [observability_config]
    assert initialized_with[0] is refreshed_with[0]
    assert session_imports == ["session", "teams"]


async def test_run_propagates_refresh_failure_before_session_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot import config as config_module
    from entrabot.harness import session as session_module
    from entrabot.harness import teams as teams_module
    from entrabot.observability import runtime, tokens

    constructed = False

    async def fail_refresh(config: EntraBotConfig) -> None:
        raise RuntimeError("refresh failed")

    class UnexpectedSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    def unexpected_token_provider() -> object:
        pytest.fail("Token provider must not be created after refresh failure")

    monkeypatch.setattr(config_module, "get_config", EntraBotConfig)
    monkeypatch.setattr(
        runtime, "initialize_observability", lambda config, *, agent_name: None
    )
    monkeypatch.setattr(tokens, "refresh_observability_token", fail_refresh)
    monkeypatch.setattr(session_module, "InteractiveSession", UnexpectedSession)
    monkeypatch.setattr(teams_module, "make_token_provider", unexpected_token_provider)
    monkeypatch.setattr(subcommands.cfgmod, "try_load", lambda root: configured_harness())

    with pytest.raises(RuntimeError, match="refresh failed"):
        await cli._cmd_run(set(), "agent-root")

    assert constructed is False


async def test_doctor_resolves_local_token_provider_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entrabot.harness import teams as teams_module

    provider = object()
    provider_calls = 0
    applied = []
    monkeypatch.setattr(subcommands, "_apply_agent_identity", applied.append)

    def make_token_provider() -> object:
        nonlocal provider_calls
        assert applied == ["agent-root"]
        provider_calls += 1
        return provider

    class FakeCopilotClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def start(self) -> None:
            pass

        async def get_auth_status(self) -> SimpleNamespace:
            return SimpleNamespace(isAuthenticated=True, login="test-user")

        async def list_models(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(id="test-model")]

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(teams_module, "make_token_provider", make_token_provider)
    monkeypatch.setattr("copilot.CopilotClient", FakeCopilotClient)

    assert await cli._cmd_doctor("agent-root") == 0
    assert provider_calls == 1
