"""Agent 365 telemetry from the MCP server itself, not only from the harness.

The host (Claude Code, Copilot CLI) owns the model loop, so the server cannot see a turn
begin and end. It sees two things: an inbound Teams message or email it pushed to the model,
and each tool call. A run (``invoke_agent``) therefore starts at an inbound push, or at the
first tool call when none is open (the operator typing in the host), and ends at the next
inbound push, after a quiet spell, or at shutdown. Each tool call is an ``execute_tool`` child
of the open run. No message text, argument or result is ever recorded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from entrabot.config import EntraBotConfig
from entrabot.observability.mcp_runs import McpRunTracker, wrap_tool_fn


@dataclass
class FakeRun:
    conversation_id: str
    channel: str
    caller_id: str | None
    finished: bool = False
    errors: list[str] = field(default_factory=list)
    tools: list[FakeTool] = field(default_factory=list)


@dataclass
class FakeTool:
    name: str
    run: FakeRun
    finished: bool = False
    error: str | None = None


class Recorder:
    def __init__(self) -> None:
        self.runs: list[FakeRun] = []

    def start_run(self, *, conversation_id: str, channel: str, caller_id: str | None) -> FakeRun:
        run = FakeRun(conversation_id, channel, caller_id)
        self.runs.append(run)
        return run

    def finish_run(self, run: FakeRun) -> None:
        run.finished = True

    def start_tool(self, run: FakeRun, tool_name: str) -> FakeTool:
        tool = FakeTool(tool_name, run)
        run.tools.append(tool)
        return tool

    def finish_tool(self, tool: FakeTool, error: BaseException | None) -> None:
        tool.finished = True
        tool.error = type(error).__name__ if error else None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _tracker(recorder: Recorder, clock: Clock, *, idle: float = 300) -> McpRunTracker:
    return McpRunTracker(
        session_id="session-1",
        idle_seconds=idle,
        clock=clock,
        start_run=recorder.start_run,
        finish_run=recorder.finish_run,
        start_tool=recorder.start_tool,
        finish_tool=recorder.finish_tool,
    )


class TestRuns:
    def test_an_inbound_teams_message_starts_a_run_its_tool_calls_join(
        self, recorder: Recorder, clock: Clock
    ) -> None:
        tracker = _tracker(recorder, clock)
        tracker.inbound(conversation_id="19:chat", channel="msteams", caller_id="user-1")
        handle = tracker.tool_started("read_teams_messages")
        tracker.tool_finished(handle, None)
        (run,) = recorder.runs
        assert (run.conversation_id, run.channel, run.caller_id) == ("19:chat", "msteams", "user-1")
        assert [tool.name for tool in run.tools] == ["read_teams_messages"]
        assert run.tools[0].finished and not run.finished

    def test_the_next_inbound_message_ends_the_previous_run(
        self, recorder: Recorder, clock: Clock
    ) -> None:
        tracker = _tracker(recorder, clock)
        tracker.inbound(conversation_id="19:a", channel="msteams", caller_id="u")
        tracker.inbound(conversation_id="19:b", channel="msteams", caller_id="u")
        assert [run.finished for run in recorder.runs] == [True, False]

    def test_a_tool_call_with_no_open_run_starts_an_operator_run(
        self, recorder: Recorder, clock: Clock
    ) -> None:
        tracker = _tracker(recorder, clock)
        tracker.tool_finished(tracker.tool_started("whoami"), None)
        (run,) = recorder.runs
        assert run.channel == "cli" and run.conversation_id == "session-1"
        assert run.caller_id is None

    def test_a_quiet_spell_ends_the_run_and_the_next_call_starts_another(
        self, recorder: Recorder, clock: Clock
    ) -> None:
        tracker = _tracker(recorder, clock, idle=60)
        tracker.tool_finished(tracker.tool_started("whoami"), None)
        clock.now += 61
        tracker.sweep()
        assert recorder.runs[0].finished
        tracker.tool_finished(tracker.tool_started("whoami"), None)
        assert len(recorder.runs) == 2

    def test_activity_keeps_a_run_open(self, recorder: Recorder, clock: Clock) -> None:
        tracker = _tracker(recorder, clock, idle=60)
        tracker.tool_finished(tracker.tool_started("a"), None)
        clock.now += 50
        tracker.tool_finished(tracker.tool_started("b"), None)
        clock.now += 50
        tracker.sweep()
        assert not recorder.runs[0].finished and len(recorder.runs) == 1

    def test_close_ends_the_open_run(self, recorder: Recorder, clock: Clock) -> None:
        tracker = _tracker(recorder, clock)
        tracker.inbound(conversation_id="19:a", channel="msteams", caller_id="u")
        tracker.close()
        assert recorder.runs[0].finished

    def test_a_failing_tool_is_recorded_as_an_error_without_its_message(
        self, recorder: Recorder, clock: Clock
    ) -> None:
        tracker = _tracker(recorder, clock)
        handle = tracker.tool_started("send_email")
        tracker.tool_finished(handle, PermissionError("secret recipient@example.com"))
        tool = recorder.runs[0].tools[0]
        assert tool.error == "PermissionError"


class TestTelemetryNeverBreaksATool:
    def test_a_telemetry_failure_is_swallowed(self, clock: Clock) -> None:
        def broken(**_kwargs: object) -> object:
            raise RuntimeError("exporter down")

        tracker = McpRunTracker(
            session_id="s",
            clock=clock,
            start_run=broken,
            finish_run=lambda run: None,
            start_tool=lambda run, name: None,
            finish_tool=lambda tool, error: None,
        )
        handle = tracker.tool_started("whoami")  # must not raise
        tracker.tool_finished(handle, None)
        tracker.inbound(conversation_id="c", channel="msteams", caller_id=None)
        tracker.close()

    async def test_the_wrapper_returns_exactly_what_the_tool_returns(
        self, recorder: Recorder, clock: Clock
    ) -> None:
        tracker = _tracker(recorder, clock)

        async def tool(x: int) -> str:
            return f"value {x}"

        wrapped = wrap_tool_fn(tracker, "tool", tool)
        assert await wrapped(x=3) == "value 3"
        assert recorder.runs[0].tools[0].name == "tool"

    async def test_the_wrapper_reraises_the_tools_exception(
        self, recorder: Recorder, clock: Clock
    ) -> None:
        tracker = _tracker(recorder, clock)

        async def tool() -> str:
            raise ValueError("refused")

        with pytest.raises(ValueError, match="refused"):
            await wrap_tool_fn(tracker, "tool", tool)()
        assert recorder.runs[0].tools[0].error == "ValueError"

    def test_sync_tools_are_wrapped_too(self, recorder: Recorder, clock: Clock) -> None:
        tracker = _tracker(recorder, clock)
        wrapped = wrap_tool_fn(tracker, "sync_tool", lambda: 42)
        assert wrapped() == 42
        assert recorder.runs[0].tools[0].finished


class TestRealScopes:
    """The default callables drive Microsoft's scopes, with content suppressed."""

    def test_default_callables_emit_invoke_agent_and_execute_tool_spans(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from entrabot.observability import mcp_runs

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(trace, "_TRACER_PROVIDER", None, raising=False)
        monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", trace.Once(), raising=False)
        trace.set_tracer_provider(provider)
        # The scopes are no-ops until the distro is initialised (use_microsoft_opentelemetry,
        # which entrabot.observability.runtime calls at boot); stand in for that here.
        from microsoft.opentelemetry.a365.core.opentelemetry_scope import OpenTelemetryScope

        monkeypatch.setattr(OpenTelemetryScope, "_enabled_by_distro", True)
        monkeypatch.setattr(OpenTelemetryScope, "_tracer", None)

        config = EntraBotConfig(
            tenant_id="tenant",
            agent_id="agent-app",
            blueprint_app_id="blueprint-app",
            agent_user_id="agent-user",
            agent_user_upn="agent@example.test",
            a365_observability_enabled=True,
        )
        tracker = mcp_runs.tracker_for(config, agent_name="agent", session_id="session-1")
        tracker.inbound(conversation_id="19:chat", channel="msteams", caller_id="user-1")
        handle = tracker.tool_started("read_teams_messages")
        tracker.tool_finished(handle, None)
        tracker.close()
        provider.force_flush()

        spans = exporter.get_finished_spans()
        operations = [span.attributes.get("gen_ai.operation.name") for span in spans]
        assert "invoke_agent" in operations and "execute_tool" in operations
        tool = next(s for s in spans if s.attributes.get("gen_ai.operation.name") == "execute_tool")
        run = next(s for s in spans if s.attributes.get("gen_ai.operation.name") == "invoke_agent")
        assert tool.parent is not None and tool.parent.span_id == run.context.span_id
        assert run.attributes.get("gen_ai.agent.id") == "agent-app"
        text = repr([dict(span.attributes) for span in spans])
        assert "user-1" in text  # the caller is attributed
        for absent in ("secret", "message body"):
            assert absent not in text


class TestServerStart:
    def _config(self, **overrides: object) -> EntraBotConfig:
        from dataclasses import replace

        base = EntraBotConfig(
            tenant_id="tenant",
            agent_id="agent-app",
            blueprint_app_id="blueprint-app",
            agent_user_id="agent-user",
            agent_user_upn="sati@example.test",
            a365_observability_enabled=True,
            a365_export_enabled=True,
        )
        return replace(base, **overrides)

    class FakeMcp:
        class Tool:
            def __init__(self, fn: Callable[..., object]) -> None:
                self.fn = fn

        def __init__(self) -> None:
            self._tool_manager = type("TM", (), {})()
            self._tool_manager._tools = {"whoami": self.Tool(lambda: "me")}

    def test_off_means_nothing_is_initialised_or_wrapped(self) -> None:
        from entrabot.observability.mcp_runs import start_server_telemetry

        mcp = self.FakeMcp()
        original = mcp._tool_manager._tools["whoami"].fn
        called: list[object] = []
        tracker = start_server_telemetry(
            mcp,
            self._config(a365_observability_enabled=False),
            session_id="s",
            initialize=lambda *a, **k: called.append(a),
        )
        assert tracker is None and called == []
        assert mcp._tool_manager._tools["whoami"].fn is original

    def test_a_delegated_session_has_no_agent_to_report_as(self) -> None:
        from entrabot.observability.mcp_runs import server_telemetry_wanted

        assert not server_telemetry_wanted(self._config(agent_user_id=None))
        assert server_telemetry_wanted(self._config())

    def test_on_initialises_the_distro_named_after_the_agent_and_wraps_tools(self) -> None:
        from entrabot.observability.mcp_runs import start_server_telemetry

        mcp = self.FakeMcp()
        names: list[str] = []
        tracker = start_server_telemetry(
            mcp,
            self._config(),
            session_id="s",
            initialize=lambda config, *, agent_name: names.append(agent_name),
        )
        assert tracker is not None and names == ["sati"]
        assert mcp._tool_manager._tools["whoami"].fn() == "me"

    def test_a_failing_distro_leaves_the_server_untouched(self) -> None:
        from entrabot.observability.mcp_runs import start_server_telemetry

        mcp = self.FakeMcp()
        original = mcp._tool_manager._tools["whoami"].fn

        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("no exporter")

        assert start_server_telemetry(mcp, self._config(), session_id="s", initialize=boom) is None
        assert mcp._tool_manager._tools["whoami"].fn is original
