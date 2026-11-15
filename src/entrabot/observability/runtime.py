import os

from opentelemetry.sdk.resources import Resource

from entrabot.config import EntraBotConfig
from entrabot.harness import __version__
from entrabot.observability.tokens import resolve_observability_token

_initialized = False

# Entrabot exports only to Agent 365 and never switches the distro's Azure Monitor export on,
# but the distro still makes two calls of its own to Azure Monitor: its usage statistics
# ("SDKStats", to an Application Insights ingestion host) and its remote-settings lookup
# (settings.sdk.monitor.azure.com). These switches, read by microsoft-opentelemetry and
# azure-monitor-opentelemetry-exporter, turn both off. An operator's own value is kept.
AZURE_MONITOR_OFF = {
    "MICROSOFT_OTEL_SDKSTATS_DISABLED": "true",
    "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL": "true",
    "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED": "true",
}


def initialize_observability(config: EntraBotConfig, *, agent_name: str) -> None:
    global _initialized

    if _initialized or not config.a365_observability_enabled:
        return

    for name, value in AZURE_MONITOR_OFF.items():
        os.environ.setdefault(name, value)

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
