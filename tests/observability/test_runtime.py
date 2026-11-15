from __future__ import annotations

import pytest

from entrabot.config import EntraBotConfig
from entrabot.harness import __version__
from entrabot.observability import runtime
from entrabot.observability.tokens import resolve_observability_token


@pytest.fixture(autouse=True)
def reset_runtime_initialization() -> None:
    runtime._initialized = False
    yield
    runtime._initialized = False


def test_disabled_initialization_does_not_call_distro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from microsoft import opentelemetry

    def unexpected_initialize(**kwargs: object) -> None:
        pytest.fail("Disabled observability must not initialize the Distro")

    monkeypatch.setattr(
        opentelemetry,
        "use_microsoft_opentelemetry",
        unexpected_initialize,
    )

    runtime.initialize_observability(
        EntraBotConfig(a365_observability_enabled=False), agent_name="fixture-agent"
    )

    assert runtime._initialized is False


@pytest.mark.parametrize("namespace", [None, "example.support"])
def test_enabled_initialization_passes_required_options_once(
    monkeypatch: pytest.MonkeyPatch,
    namespace: str | None,
) -> None:
    from microsoft import opentelemetry

    calls: list[dict[str, object]] = []

    def fake_initialize(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        opentelemetry,
        "use_microsoft_opentelemetry",
        fake_initialize,
    )
    config = EntraBotConfig(
        a365_observability_enabled=True,
        a365_export_enabled=True,
        a365_console_enabled=True,
        a365_service_namespace=namespace,
    )

    runtime.initialize_observability(config, agent_name="fixture-agent")
    runtime.initialize_observability(config, agent_name="fixture-agent")

    assert len(calls) == 1
    options = calls[0]
    resource = options.pop("resource")
    assert resource.attributes["service.name"] == "fixture-agent"
    if namespace is None:
        assert "service.namespace" not in resource.attributes
    else:
        assert resource.attributes["service.namespace"] == namespace
    assert resource.attributes["service.version"] == __version__
    assert options == {
        "enable_a365": True,
        "a365_token_resolver": resolve_observability_token,
        "a365_use_s2s_endpoint": False,
        "a365_enable_observability_exporter": True,
        "a365_suppress_invoke_agent_input": True,
        "enable_console": True,
        "instrumentation_options": {"openai_agents": {"enabled": False}},
    }
    assert runtime._initialized is True


def test_failed_initialization_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from microsoft import opentelemetry

    attempts = 0

    def flaky_initialize(**kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Distro failed")

    monkeypatch.setattr(
        opentelemetry,
        "use_microsoft_opentelemetry",
        flaky_initialize,
    )
    config = EntraBotConfig(a365_observability_enabled=True)

    with pytest.raises(RuntimeError, match="Distro failed"):
        runtime.initialize_observability(config, agent_name="fixture-agent")

    assert runtime._initialized is False

    runtime.initialize_observability(config, agent_name="fixture-agent")

    assert attempts == 2
    assert runtime._initialized is True


_AZURE_MONITOR_SWITCHES = (
    "MICROSOFT_OTEL_SDKSTATS_DISABLED",
    "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL",
    "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED",
)


def _initialize_with_fake_distro(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    import os

    from microsoft import opentelemetry

    seen: dict[str, str | None] = {}

    def fake_initialize(**kwargs: object) -> None:
        seen.update({name: os.environ.get(name) for name in _AZURE_MONITOR_SWITCHES})

    monkeypatch.setattr(opentelemetry, "use_microsoft_opentelemetry", fake_initialize)
    runtime.initialize_observability(
        EntraBotConfig(a365_observability_enabled=True), agent_name="fixture-agent"
    )
    return seen


def test_the_distros_azure_monitor_calls_are_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Entrabot exports only to Agent 365, but the distro also sends its own usage statistics
    # to Application Insights and fetches settings from settings.sdk.monitor.azure.com.
    for name in _AZURE_MONITOR_SWITCHES:
        monkeypatch.setenv(name, "unset")  # so teardown removes what the code sets
        monkeypatch.delenv(name)
    assert _initialize_with_fake_distro(monkeypatch) == dict.fromkeys(
        _AZURE_MONITOR_SWITCHES, "true"
    )


def test_an_operators_own_setting_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _AZURE_MONITOR_SWITCHES:
        monkeypatch.setenv(name, "false")
    assert _initialize_with_fake_distro(monkeypatch) == dict.fromkeys(
        _AZURE_MONITOR_SWITCHES, "false"
    )
