from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from types import TracebackType

import pytest
from microsoft.opentelemetry.a365.core import (
    AgentDetails,
    CallerDetails,
    InvokeAgentScopeDetails,
    Request,
)

from entrabot.config import EntraBotConfig
from entrabot.observability import context as context_module


@dataclass
class BaggageValues:
    tenant_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    agent_blueprint_id: str | None = None
    agentic_user_id: str | None = None
    agentic_user_email: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    channel_name: str | None = None
    user_id: str | None = None
    user_client_ip: str | None = None
    server: tuple[str, int] | None = None


@dataclass
class ScopeStart:
    request: Request
    scope_details: InvokeAgentScopeDetails
    agent_details: AgentDetails
    caller_details: CallerDetails | None


@dataclass
class TelemetryRecorder:
    events: list[str] = field(default_factory=list)
    baggage: list[BaggageValues] = field(default_factory=list)
    starts: list[ScopeStart] = field(default_factory=list)
    scopes: list[RecordingScope] = field(default_factory=list)
    active_baggage: int = 0
    fail_scope_start: bool = False


class RecordingBaggageScope:
    def __init__(self, recorder: TelemetryRecorder) -> None:
        self._recorder = recorder

    def __enter__(self) -> RecordingBaggageScope:
        self._recorder.events.append("baggage_enter")
        self._recorder.active_baggage += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._recorder.events.append("baggage_exit")
        self._recorder.active_baggage -= 1


class RecordingBaggageBuilder:
    def __init__(self, recorder: TelemetryRecorder) -> None:
        self._recorder = recorder
        self.values = BaggageValues()
        recorder.baggage.append(self.values)

    def tenant_id(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.tenant_id = value
        return self

    def agent_id(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.agent_id = value
        return self

    def agent_name(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.agent_name = value
        return self

    def agent_blueprint_id(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.agent_blueprint_id = value
        return self

    def agentic_user_id(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.agentic_user_id = value
        return self

    def agentic_user_email(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.agentic_user_email = value
        return self

    def conversation_id(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.conversation_id = value
        return self

    def session_id(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.session_id = value
        return self

    def channel_name(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.channel_name = value
        return self

    def user_id(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.user_id = value
        return self

    def user_client_ip(self, value: str | None) -> RecordingBaggageBuilder:
        self.values.user_client_ip = value
        return self

    def invoke_agent_server(
        self,
        address: str | None,
        port: int | None = None,
    ) -> RecordingBaggageBuilder:
        assert address is not None
        assert port is not None
        self.values.server = (address, port)
        return self

    def build(self) -> RecordingBaggageScope:
        return RecordingBaggageScope(self._recorder)


class RecordingScope:
    def __init__(self, recorder: TelemetryRecorder) -> None:
        self._recorder = recorder
        self.errors: list[Exception] = []
        self.responses: list[str] = []
        self.dispose_count = 0

    def __enter__(self) -> RecordingScope:
        pytest.fail("InvokeAgentScope must not be entered across callbacks")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pytest.fail("InvokeAgentScope must not be exited across callbacks")

    def dispose(self) -> None:
        self.dispose_count += 1
        self._recorder.events.append("scope_dispose")

    def record_error(self, error: Exception) -> None:
        self.errors.append(error)

    def record_response(self, response: str) -> None:
        self.responses.append(response)
        self._recorder.events.append("response")


@pytest.fixture
def telemetry_recorder(monkeypatch: pytest.MonkeyPatch) -> TelemetryRecorder:
    recorder = TelemetryRecorder()

    class BaggageBuilderFactory(RecordingBaggageBuilder):
        def __init__(self) -> None:
            super().__init__(recorder)

    class InvokeAgentScopeFactory:
        @staticmethod
        def start(
            request: Request,
            scope_details: InvokeAgentScopeDetails,
            agent_details: AgentDetails,
            caller_details: CallerDetails | None,
        ) -> RecordingScope:
            recorder.events.append("scope_start")
            assert recorder.active_baggage == 1
            if recorder.fail_scope_start:
                raise RuntimeError("scope start failed")
            recorder.starts.append(
                ScopeStart(request, scope_details, agent_details, caller_details)
            )
            scope = RecordingScope(recorder)
            recorder.scopes.append(scope)
            return scope

    monkeypatch.setattr(context_module, "BaggageBuilder", BaggageBuilderFactory)
    monkeypatch.setattr(context_module, "InvokeAgentScope", InvokeAgentScopeFactory)
    return recorder


def enabled_config(**overrides: object) -> EntraBotConfig:
    values: dict[str, object] = {
        "a365_observability_enabled": True,
        "tenant_id": "tenant-id",
        "agent_id": "agent-id",
        "blueprint_app_id": "blueprint-id",
        "agent_user_id": "agent-user-id",
        "agent_user_upn": "agent@example.com",
        "human_user_id": "human-id",
    }
    values.update(overrides)
    return EntraBotConfig(**values)


def test_disabled_is_noop_without_touching_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnexpectedSdkCall:
        def __init__(self) -> None:
            pytest.fail("A365 SDK must not be touched when observability is disabled")

    monkeypatch.setattr(context_module, "BaggageBuilder", UnexpectedSdkCall)
    monkeypatch.setattr(context_module, "InvokeAgentScope", UnexpectedSdkCall)

    invocation = context_module.start_invocation(
        config=EntraBotConfig(a365_observability_enabled=False),
        agent_name="Entrabot",
        session_id=None,
        conversation_id=None,
        channel="Teams",
        caller_id=None,
    )

    assert invocation is None


def test_missing_required_identity_fields_are_named(
    telemetry_recorder: TelemetryRecorder,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        context_module.start_invocation(
            config=EntraBotConfig(a365_observability_enabled=True),
            agent_name=" ",
            session_id=None,
            conversation_id=None,
            channel="",
            caller_id=None,
        )

    message = str(exc_info.value)
    for field_name in (
        "tenant_id",
        "agent_id",
        "blueprint_app_id",
        "agent_user_id",
        "agent_name",
        "conversation_id",
        "channel",
    ):
        assert field_name in message
    assert telemetry_recorder.baggage == []
    assert telemetry_recorder.starts == []


@pytest.mark.parametrize("human_user_id", [None, "", "   "])
def test_cli_without_human_identity_still_traces_the_agent(
    telemetry_recorder: TelemetryRecorder,
    human_user_id: str | None,
) -> None:
    invocation = context_module.start_invocation(
        config=enabled_config(human_user_id=human_user_id),
        agent_name="fixture-cli-agent",
        session_id="session-id",
        conversation_id="conversation-id",
        channel="entrabot-cli",
        caller_id=None,
    )

    assert invocation is not None
    assert telemetry_recorder.starts[0].caller_details is None
    assert telemetry_recorder.starts[0].agent_details.agent_id == "agent-id"
    assert telemetry_recorder.baggage[0].user_id is None
    context_module.finish_invocation(invocation)
    assert telemetry_recorder.scopes[0].dispose_count == 1


def test_cli_event_emits_real_sdk_span_without_configured_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    import copilot
    from microsoft.opentelemetry.a365.core import InvokeAgentScope
    from microsoft.opentelemetry.a365.core.constants import (
        ENABLE_A365_OBSERVABILITY,
        ENABLE_OBSERVABILITY,
        GEN_AI_AGENT_ID_KEY,
        USER_ID_KEY,
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from entrabot.harness.config import HarnessConfig
    from entrabot.harness.session import InteractiveSession, core

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(InvokeAgentScope, "_tracer", provider.get_tracer("cli-regression"))
    monkeypatch.setenv(ENABLE_OBSERVABILITY, "true")
    monkeypatch.setenv(ENABLE_A365_OBSERVABILITY, "true")
    monkeypatch.setattr(core, "get_config", lambda: enabled_config(human_user_id=None))
    ui = Mock()
    session = InteractiveSession(
        HarnessConfig(name="fixture-cli-agent", description="test", agent_id="session-id"),
        ".",
        ui,
    )
    try:
        session._on_event(
            SimpleNamespace(
                type=copilot.SessionEventType.USER_MESSAGE,
                data=SimpleNamespace(content="fixture CLI input"),
            )
        )
        session._on_event(
            SimpleNamespace(
                type=copilot.SessionEventType.SESSION_IDLE,
                data=SimpleNamespace(),
            )
        )

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "invoke_agent fixture-cli-agent"
        assert spans[0].attributes[GEN_AI_AGENT_ID_KEY] == "agent-id"
        assert USER_ID_KEY not in spans[0].attributes
        assert not any(
            "A365 observability failed" in call.args[0] for call in ui.append_line.call_args_list
        )
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    ("channel", "expected_ip"),
    [
        ("Teams", "0.0.0.0"),
        ("msteams", "0.0.0.0"),
        ("local-cli", "127.0.0.1"),
    ],
)
def test_start_populates_suppressed_request_and_identity_context(
    monkeypatch: pytest.MonkeyPatch,
    telemetry_recorder: TelemetryRecorder,
    channel: str,
    expected_ip: str,
) -> None:
    monkeypatch.setattr(context_module.socket, "gethostname", lambda: "host-name")

    invocation = context_module.start_invocation(
        config=enabled_config(),
        agent_name="Entrabot",
        session_id="session-id",
        conversation_id="conversation-id",
        channel=channel,
        caller_id=None,
    )

    assert invocation is not None
    assert invocation.scope is telemetry_recorder.scopes[0]
    assert telemetry_recorder.baggage == [
        BaggageValues(
            tenant_id="tenant-id",
            agent_id="agent-id",
            agent_name="Entrabot",
            agent_blueprint_id="blueprint-id",
            agentic_user_id="agent-user-id",
            agentic_user_email="agent@example.com",
            conversation_id="conversation-id",
            session_id="session-id",
            channel_name=channel,
            user_id="human-id",
            user_client_ip=expected_ip,
            server=("host-name", 0),
        )
    ]

    started = telemetry_recorder.starts[0]
    assert started.request.content == context_module.SUPPRESSED_CONTENT
    assert started.request.session_id == "session-id"
    assert started.request.conversation_id == "conversation-id"
    assert started.request.channel is not None
    assert started.request.channel.name == channel
    assert started.agent_details == AgentDetails(
        agent_id="agent-id",
        agent_name="Entrabot",
        agentic_user_id="agent-user-id",
        agentic_user_email="agent@example.com",
        agent_blueprint_id="blueprint-id",
        tenant_id="tenant-id",
    )
    assert started.caller_details.user_details is not None
    assert started.caller_details.user_details.user_id == "human-id"
    assert started.caller_details.user_details.user_email is None
    assert started.caller_details.user_details.user_client_ip == expected_ip
    assert started.scope_details.endpoint is not None
    assert started.scope_details.endpoint.hostname == "host-name"
    assert started.scope_details.endpoint.port == 0
    assert telemetry_recorder.active_baggage == 0
    assert telemetry_recorder.events == [
        "baggage_enter",
        "scope_start",
        "baggage_exit",
    ]


def test_error_recording_and_finish_are_idempotent(
    telemetry_recorder: TelemetryRecorder,
) -> None:
    invocation = context_module.start_invocation(
        config=enabled_config(),
        agent_name="Entrabot",
        session_id=None,
        conversation_id="conversation-id",
        channel="Teams",
        caller_id="explicit-caller",
    )
    assert invocation is not None
    scope = telemetry_recorder.scopes[0]
    error = RuntimeError("SENSITIVE-PROVIDER-RESPONSE")

    context_module.record_invocation_error(invocation, error)
    context_module.record_invocation_error(None, error)
    context_module.finish_invocation(invocation)
    context_module.finish_invocation(invocation)
    context_module.finish_invocation(None)

    assert len(scope.errors) == 1
    assert str(scope.errors[0]) == "Agent invocation failed (details suppressed)"
    assert scope.errors[0].__cause__ is None
    assert scope.errors[0].__context__ is None
    assert scope.responses == [context_module.SUPPRESSED_CONTENT]
    assert scope.dispose_count == 1
    assert telemetry_recorder.baggage[0].user_id == "explicit-caller"
    assert telemetry_recorder.events == [
        "baggage_enter",
        "scope_start",
        "baggage_exit",
        "response",
        "scope_dispose",
    ]


def test_scope_start_failure_exits_baggage_and_propagates(
    telemetry_recorder: TelemetryRecorder,
) -> None:
    telemetry_recorder.fail_scope_start = True

    with pytest.raises(RuntimeError, match="scope start failed"):
        context_module.start_invocation(
            config=enabled_config(),
            agent_name="Entrabot",
            session_id=None,
            conversation_id="conversation-id",
            channel="Teams",
            caller_id=None,
        )

    assert telemetry_recorder.active_baggage == 0
    assert telemetry_recorder.events == [
        "baggage_enter",
        "scope_start",
        "baggage_exit",
    ]


def test_invocation_lifecycle_is_safe_across_callback_contexts(
    telemetry_recorder: TelemetryRecorder,
) -> None:
    invocation = contextvars.Context().run(
        context_module.start_invocation,
        config=enabled_config(),
        agent_name="Entrabot",
        session_id="session-id",
        conversation_id="conversation-id",
        channel="Teams",
        caller_id=None,
    )

    assert invocation is not None
    assert telemetry_recorder.active_baggage == 0
    contextvars.Context().run(context_module.finish_invocation, invocation)

    assert telemetry_recorder.events == [
        "baggage_enter",
        "scope_start",
        "baggage_exit",
        "response",
        "scope_dispose",
    ]
    assert telemetry_recorder.scopes[0].dispose_count == 1
