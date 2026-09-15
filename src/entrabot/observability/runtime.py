from opentelemetry.sdk.resources import Resource

from entrabot.config import EntraBotConfig
from entrabot.harness import __version__
from entrabot.observability.tokens import resolve_observability_token

_initialized = False


def initialize_observability(config: EntraBotConfig, *, agent_name: str) -> None:
    global _initialized

    if _initialized or not config.a365_observability_enabled:
        return

    from microsoft.opentelemetry import use_microsoft_opentelemetry

    attributes = {"service.name": agent_name, "service.version": __version__}
    if config.a365_service_namespace:
        attributes["service.namespace"] = config.a365_service_namespace

    use_microsoft_opentelemetry(
        resource=Resource.create(attributes),
        enable_a365=True,
        a365_token_resolver=resolve_observability_token,
        a365_use_s2s_endpoint=False,
        a365_enable_observability_exporter=config.a365_export_enabled,
        a365_suppress_invoke_agent_input=True,
        enable_console=config.a365_console_enabled,
        instrumentation_options={"openai_agents": {"enabled": False}},
    )
    _initialized = True
