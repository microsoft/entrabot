from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import copilot
import pytest

from entrabot.config import EntraBotConfig
from entrabot.harness.config import HarnessConfig
from entrabot.harness.session import InteractiveSession
from entrabot.harness.session import core as core_module
from entrabot.harness.session import events as events_module
from entrabot.harness.ui import UiStyle


class FakeUI:
    def __init__(self) -> None:
        self.lines: list[tuple[str, UiStyle | None]] = []
        self.working: list[bool] = []

    def append_line(self, text: str, style: UiStyle | None = None) -> None:
        self.lines.append((text, style))

    def set_working(self, working: bool) -> None:
        self.working.append(working)


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> InteractiveSession:
    observability_config = EntraBotConfig(
        a365_observability_enabled=True,
        human_user_id="operator-id",
    )
    monkeypatch.setattr(core_module, "get_config", lambda: observability_config, raising=False)
    return InteractiveSession(
        HarnessConfig(
            name="Research Agent",
            description="test agent",
            agent_id="session-id",
        ),
        ".",
        FakeUI(),
    )


def event(event_type: copilot.SessionEventType, **data: object) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, data=SimpleNamespace(**data))


@pytest.mark.parametrize(
    "caller,chat", [(None, None), ("sponsor", "teams-chat"), ("guest", "chat")],
)
async def test_all_accepted_turns_become_busy_and_can_be_interrupted(
    monkeypatch, session, caller, chat,
):
    session._session = Mock(send=AsyncMock(), abort=AsyncMock())
    monkeypatch.setattr(events_module, "start_invocation", Mock(return_value=None))
    await session._inject("fixture-prompt", caller, chat)
    session._on_event(event(copilot.SessionEventType.USER_MESSAGE, content="fixture-prompt"))

    assert not session._idle.is_set()
    assert session._ui.working[-1] is True
    await session._interrupt()
    session._session.abort.assert_awaited_once()
    session._on_event(event(copilot.SessionEventType.SESSION_IDLE))
    assert session._idle.is_set()
    assert session._ui.working[-1] is False


def test_session_errors_export_failure_but_not_provider_content(monkeypatch):
    from microsoft.opentelemetry.a365.core import InvokeAgentScope
    from microsoft.opentelemetry.a365.core.opentelemetry_scope import OpenTelemetryScope
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(InvokeAgentScope, "_tracer", provider.get_tracer("offline-fixture"))
    monkeypatch.setattr(OpenTelemetryScope, "_enabled_by_distro", True)
    config = EntraBotConfig(
        a365_observability_enabled=True,
        tenant_id="fixture-tenant",
        agent_id="fixture-agent",
        blueprint_app_id="fixture-blueprint",
        agent_user_id="fixture-user",
    )
    monkeypatch.setattr(core_module, "get_config", lambda: config)
    runtime = InteractiveSession(
        HarnessConfig(name="fixture", description="fixture", agent_id="fixture-session"),
        ".", FakeUI(),
    )
    marker = "synthetic-sensitive-provider-content"
    try:
        runtime._on_event(event(copilot.SessionEventType.USER_MESSAGE, content="fixture request"))
        runtime._on_event(event(copilot.SessionEventType.SESSION_ERROR, message=marker))
        runtime._on_event(event(copilot.SessionEventType.SESSION_IDLE))

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code == StatusCode.ERROR
        assert marker not in str(span.attributes)
        assert all(marker not in str(entry.attributes) for entry in span.events)
        assert marker not in (span.status.description or "")
    finally:
        provider.shutdown()


def test_user_message_binds_before_starting_teams_invocation(
    monkeypatch: pytest.MonkeyPatch,
    session: InteractiveSession,
) -> None:
    invocation = object()
    calls: list[dict[str, object]] = []
    session._injected["hello"] = ("caller-id", "chat-id")

    def fake_start(**kwargs: object) -> object:
        assert session._ctx.caller == "caller-id"
        assert session._ctx.chat == "chat-id"
        calls.append(kwargs)
        return invocation

    monkeypatch.setattr(events_module, "start_invocation", fake_start, raising=False)

    session._on_event(event(copilot.SessionEventType.USER_MESSAGE, content="hello"))

    assert calls == [
        {
            "config": session._observability_config,
            "agent_name": "Research Agent",
            "session_id": "session-id",
            "conversation_id": "chat-id",
            "channel": "msteams",
            "caller_id": "caller-id",
        }
    ]
    assert session._invocation_scope is invocation
    assert "hello" not in session._injected


def test_cli_user_message_uses_session_conversation_and_no_caller(
    monkeypatch: pytest.MonkeyPatch,
    session: InteractiveSession,
) -> None:
    calls: list[dict[str, object]] = []
    session._ctx.caller = "stale-caller"
    session._ctx.chat = "stale-chat"
    session._injected["local"] = (None, None)
    monkeypatch.setattr(
        events_module,
        "start_invocation",
        lambda **kwargs: calls.append(kwargs) or object(),
        raising=False,
    )

    session._on_event(event(copilot.SessionEventType.USER_MESSAGE, content="local"))

    assert calls == [
        {
            "config": session._observability_config,
            "agent_name": "Research Agent",
            "session_id": "session-id",
            "conversation_id": "session-id",
            "channel": "entrabot-cli",
            "caller_id": None,
        }
    ]


def test_second_user_message_steers_active_invocation_without_nesting(
    monkeypatch: pytest.MonkeyPatch,
    session: InteractiveSession,
) -> None:
    invocation = object()
    calls: list[dict[str, object]] = []
    session._injected.update(
        {
            "first": ("caller-one", "chat-one"),
            "second": ("caller-two", "chat-two"),
        }
    )
    monkeypatch.setattr(
        events_module,
        "start_invocation",
        lambda **kwargs: calls.append(kwargs) or invocation,
        raising=False,
    )

    session._on_event(event(copilot.SessionEventType.USER_MESSAGE, content="first"))
    session._on_event(event(copilot.SessionEventType.USER_MESSAGE, content="second"))

    assert len(calls) == 1
    assert session._invocation_scope is invocation
    assert session._ctx.caller == "caller-two"
    assert session._ctx.chat == "chat-two"


def test_start_failure_is_visible_and_event_handling_continues(
    monkeypatch: pytest.MonkeyPatch,
    session: InteractiveSession,
) -> None:
    session._injected["hello"] = ("caller-id", "chat-id")

    def fail_start(**kwargs: object) -> None:
        raise ValueError("bad telemetry")

    monkeypatch.setattr(events_module, "start_invocation", fail_start, raising=False)

    session._on_event(event(copilot.SessionEventType.USER_MESSAGE, content="hello"))

    assert session._ctx.caller == "caller-id"
    assert session._ctx.chat == "chat-id"
    assert session._invocation_scope is None
    assert (
        "A365 observability failed: ValueError: bad telemetry",
        UiStyle.WARN,
    ) in session._ui.lines


def test_session_error_records_failure_and_preserves_error_rendering(
    monkeypatch: pytest.MonkeyPatch,
    session: InteractiveSession,
) -> None:
    invocation = object()
    recorded: list[tuple[object, Exception]] = []
    session._invocation_scope = invocation
    monkeypatch.setattr(
        events_module,
        "record_invocation_error",
        lambda scope, error: recorded.append((scope, error)),
        raising=False,
    )

    session._on_event(event(copilot.SessionEventType.SESSION_ERROR, message="run failed"))

    assert len(recorded) == 1
    assert recorded[0][0] is invocation
    assert isinstance(recorded[0][1], RuntimeError)
    assert str(recorded[0][1]) == "run failed"
    assert ("run failed", UiStyle.ERROR) in session._ui.lines


def test_record_failure_is_visible_and_preserves_error_rendering(
    monkeypatch: pytest.MonkeyPatch,
    session: InteractiveSession,
) -> None:
    session._invocation_scope = object()

    def fail_record(scope: object, error: Exception) -> None:
        raise RuntimeError("record unavailable")

    monkeypatch.setattr(
        events_module,
        "record_invocation_error",
        fail_record,
        raising=False,
    )

    session._on_event(event(copilot.SessionEventType.SESSION_ERROR, message="run failed"))

    assert ("run failed", UiStyle.ERROR) in session._ui.lines
    assert (
        "A365 observability failed: RuntimeError: record unavailable",
        UiStyle.WARN,
    ) in session._ui.lines


def test_session_idle_clears_scope_and_state_when_finish_fails(
    monkeypatch: pytest.MonkeyPatch,
    session: InteractiveSession,
) -> None:
    invocation = object()
    finished: list[object] = []
    session._invocation_scope = invocation
    session._ctx.caller = "caller-id"
    session._ctx.chat = "chat-id"
    session._idle.clear()

    def fail_finish(scope: object) -> None:
        finished.append(scope)
        raise LookupError("export failed")

    monkeypatch.setattr(events_module, "finish_invocation", fail_finish, raising=False)

    session._on_event(event(copilot.SessionEventType.SESSION_IDLE))

    assert finished == [invocation]
    assert session._invocation_scope is None
    assert session._ctx.caller is None
    assert session._ctx.chat is None
    assert session._idle.is_set()
    assert session._ui.working[-1] is False
    assert (
        "A365 observability failed: LookupError: export failed",
        UiStyle.WARN,
    ) in session._ui.lines


async def test_dispose_closes_active_invocation_and_continues_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    session: InteractiveSession,
) -> None:
    cleanup: list[str] = []
    invocation = object()
    recorded: list[tuple[object, Exception]] = []
    finished: list[object] = []
    session._invocation_scope = invocation

    class AsyncCleanup:
        def __init__(self, label: str) -> None:
            self._label = label

        async def stop(self) -> None:
            cleanup.append(self._label)

        async def disconnect(self) -> None:
            cleanup.append(self._label)

    def fail_record(scope: object, error: Exception) -> None:
        recorded.append((scope, error))
        raise RuntimeError("record failed")

    monkeypatch.setattr(core_module, "record_invocation_error", fail_record, raising=False)
    monkeypatch.setattr(
        core_module,
        "finish_invocation",
        lambda scope: finished.append(scope),
        raising=False,
    )
    session._bridge = AsyncCleanup("bridge")
    session._scheduler = AsyncCleanup("scheduler")
    session._session = AsyncCleanup("session")
    session._client = AsyncCleanup("client")

    await session._dispose()

    assert session._invocation_scope is None
    assert len(recorded) == 1
    assert recorded[0][0] is invocation
    assert isinstance(recorded[0][1], RuntimeError)
    assert str(recorded[0][1]) == "Harness stopped before the invocation became idle"
    assert finished == [invocation]
    assert cleanup == ["bridge", "scheduler", "session", "client"]
    assert (
        "A365 observability failed: RuntimeError: record failed",
        UiStyle.WARN,
    ) in session._ui.lines
