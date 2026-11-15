"""Agent 365 telemetry emitted by the MCP server itself.

The harness (``entrabot.harness``) owns its model loop and opens one ``invoke_agent`` span per
turn. Under Claude Code or Copilot CLI the host owns the loop, so the server cannot see a turn
begin or end. It sees two things, and the spans follow them:

- An inbound Teams message or email that it pushed to the model starts a run
  (``invoke_agent``), attributed to the sender, in that conversation and channel.
- A tool call joins the open run as an ``execute_tool`` child. With no run open (the operator
  typing in the host) it starts one in the ``cli`` channel, for this server session.
- A run ends at the next inbound push, after ``idle_seconds`` with no tool call, or when the
  server stops.

Content is never recorded: requests carry ``[content suppressed]`` and tool spans carry only
the tool's name, never its arguments or result, and an error only as "failed". Telemetry
never breaks a tool: every failure in here is logged and swallowed.

Microsoft keeps the spans only when someone in the tenant has a Microsoft 365 E7 or Agent 365
licence assigned (Microsoft Learn, "Agent 365 observability concepts").
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from threading import RLock
from typing import Any

from entrabot.config import EntraBotConfig

log = logging.getLogger(__name__)

DEFAULT_IDLE_SECONDS = 300.0
CLI_CHANNEL = "cli"


class McpRunTracker:
    """Which run is open, and the spans for each tool call. The four callables do the
    OpenTelemetry work (see :func:`tracker_for`); tests pass fakes."""

    def __init__(
        self,
        *,
        session_id: str,
        start_run: Callable[..., Any],
        finish_run: Callable[[Any], None],
        start_tool: Callable[[Any, str], Any],
        finish_tool: Callable[[Any, BaseException | None], None],
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_id = session_id
        self._start_run = start_run
        self._finish_run = finish_run
        self._start_tool = start_tool
        self._finish_tool = finish_tool
        self._idle = idle_seconds
        self._clock = clock
        self._lock = RLock()
        self._run: Any = None
        self._last_activity = 0.0

    def _end_run_locked(self) -> None:
        run, self._run = self._run, None
        if run is not None:
            try:
                self._finish_run(run)
            except Exception as exc:  # noqa: BLE001 - telemetry never breaks the agent
                log.warning("Agent 365 telemetry: ending a run failed: %s", type(exc).__name__)

    def _begin_run_locked(
        self, *, conversation_id: str, channel: str, caller_id: str | None
    ) -> None:
        try:
            self._run = self._start_run(
                conversation_id=conversation_id, channel=channel, caller_id=caller_id
            )
        except Exception as exc:  # noqa: BLE001
            self._run = None
            log.warning("Agent 365 telemetry: starting a run failed: %s", type(exc).__name__)
        self._last_activity = self._clock()

    def inbound(self, *, conversation_id: str, channel: str, caller_id: str | None) -> None:
        """A message was pushed to the model: the previous run ends, this one starts."""
        with self._lock:
            self._end_run_locked()
            self._begin_run_locked(
                conversation_id=conversation_id, channel=channel, caller_id=caller_id
            )

    def sweep(self) -> None:
        """End the open run when nothing has happened in it for ``idle_seconds``."""
        with self._lock:
            if self._run is not None and self._clock() - self._last_activity > self._idle:
                self._end_run_locked()

    def tool_started(self, tool_name: str) -> Any:
        """The span for one tool call (None when telemetry could not start it)."""
        with self._lock:
            self.sweep()
            if self._run is None:
                self._begin_run_locked(
                    conversation_id=self._session_id, channel=CLI_CHANNEL, caller_id=None
                )
            self._last_activity = self._clock()
            if self._run is None:
                return None
            try:
                return self._start_tool(self._run, tool_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("Agent 365 telemetry: tool span failed: %s", type(exc).__name__)
                return None

    def tool_finished(self, handle: Any, error: BaseException | None) -> None:
        with self._lock:
            self._last_activity = self._clock()
        if handle is None:
            return
        try:
            self._finish_tool(handle, error)
        except Exception as exc:  # noqa: BLE001
            log.warning("Agent 365 telemetry: ending a tool span failed: %s", type(exc).__name__)

    def close(self) -> None:
        with self._lock:
            self._end_run_locked()


def wrap_tool_fn(tracker: McpRunTracker, tool_name: str, fn: Callable) -> Callable:
    """*fn* with an ``execute_tool`` span around each call. The return value and any
    exception reach the caller unchanged."""
    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            handle = tracker.tool_started(tool_name)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                tracker.tool_finished(handle, exc)
                raise
            tracker.tool_finished(handle, None)
            return result

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        handle = tracker.tool_started(tool_name)
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            tracker.tool_finished(handle, exc)
            raise
        tracker.tool_finished(handle, None)
        return result

    return sync_wrapper


def install_into_fastmcp(mcp: Any, tracker: McpRunTracker) -> int:
    """Wrap every registered tool on *mcp*. Returns how many were wrapped."""
    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None)
    if not isinstance(tools, dict):
        log.warning("Agent 365 telemetry: FastMCP has no tool manager; tools are not traced")
        return 0
    for name, tool in list(tools.items()):
        tool.fn = wrap_tool_fn(tracker, name, tool.fn)
    return len(tools)


def tracker_for(
    config: EntraBotConfig,
    *,
    agent_name: str,
    session_id: str,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
) -> McpRunTracker:
    """A tracker whose runs and tool calls are Microsoft's Agent 365 scopes, built with the
    content-suppressing helpers PR #122 added for the harness (``observability.context``)."""
    from microsoft.opentelemetry.a365.core import (
        AgentDetails,
        Channel,
        ExecuteToolScope,
        Request,
        SpanDetails,
        ToolCallDetails,
    )

    from entrabot.observability.context import (
        SUPPRESSED_CONTENT,
        finish_invocation,
        start_invocation,
    )

    def start_run(*, conversation_id: str, channel: str, caller_id: str | None) -> Any:
        invocation = start_invocation(
            config=config,
            agent_name=agent_name,
            session_id=session_id,
            conversation_id=conversation_id,
            channel=channel,
            caller_id=caller_id,
        )
        return (invocation, conversation_id, channel)

    def finish_run(run: Any) -> None:
        finish_invocation(run[0])

    def start_tool(run: Any, tool_name: str) -> Any:
        invocation, conversation_id, channel = run
        if invocation is None:
            return None
        agent = AgentDetails(
            agent_id=config.agent_id,
            agent_name=agent_name,
            tenant_id=config.tenant_id,
            agent_blueprint_id=config.blueprint_app_id,
            agentic_user_id=config.agent_user_id,
            agentic_user_email=config.agent_user_upn,
        )
        request = Request(
            content=SUPPRESSED_CONTENT,
            session_id=session_id,
            conversation_id=conversation_id,
            channel=Channel(name=channel),
        )
        return ExecuteToolScope.start(
            request,
            ToolCallDetails(tool_name=tool_name),
            agent,
            span_details=SpanDetails(parent_context=invocation.scope.get_context()),
        )

    def finish_tool(scope: Any, error: BaseException | None) -> None:
        if scope is None:
            return
        try:
            if error is not None:
                scope.record_error(RuntimeError("Tool call failed (details suppressed)"))
        finally:
            scope.dispose()

    return McpRunTracker(
        session_id=session_id,
        start_run=start_run,
        finish_run=finish_run,
        start_tool=start_tool,
        finish_tool=finish_tool,
        idle_seconds=idle_seconds,
    )


SWEEP_SECONDS = 30.0
_REQUIRED = ("tenant_id", "agent_id", "blueprint_app_id", "agent_user_id")


def server_telemetry_wanted(config: EntraBotConfig) -> bool:
    """Telemetry from the MCP server: switched on, and an Agent User session to attribute it
    to (a delegated session has no agent identity to report as)."""
    return config.a365_observability_enabled and all(
        (getattr(config, name) or "").strip() for name in _REQUIRED
    )


def agent_name_for(config: EntraBotConfig) -> str:
    """``service.name`` and the agent name in the spans: the Agent User's sign-in name."""
    return (config.agent_user_upn or "").partition("@")[0].strip() or "entrabot"


def start_server_telemetry(
    mcp: Any,
    config: EntraBotConfig,
    *,
    session_id: str,
    initialize: Callable[..., None] | None = None,
) -> McpRunTracker | None:
    """Initialise the Microsoft OpenTelemetry Distro and trace every tool on *mcp*. Returns
    the tracker, or None when telemetry is off or could not start (logged, never raised)."""
    if not server_telemetry_wanted(config):
        return None
    try:
        if initialize is None:
            from entrabot.observability.runtime import initialize_observability as initialize
        agent_name = agent_name_for(config)
        initialize(config, agent_name=agent_name)
        tracker = tracker_for(config, agent_name=agent_name, session_id=session_id)
        count = install_into_fastmcp(mcp, tracker)
    except Exception as exc:  # noqa: BLE001 - the server runs without telemetry
        log.warning("Agent 365 telemetry did not start: %s: %s", type(exc).__name__, exc)
        return None
    log.info("Agent 365 telemetry on: %d tools traced, content suppressed", count)
    return tracker


async def run_sweeper(tracker: McpRunTracker, *, every: float = SWEEP_SECONDS) -> None:
    """End quiet runs until cancelled, so their spans are exported without waiting for the
    next message."""
    while True:
        await asyncio.sleep(every)
        tracker.sweep()


async def keep_token_fresh(config: EntraBotConfig) -> None:
    """Fetch the agent's telemetry token now, then keep it fresh (PR #122's refresh loop).
    The exporter's resolver only reads this cache, so without it nothing is sent."""
    from entrabot.observability.tokens import (
        refresh_observability_token,
        run_observability_token_refresh,
    )

    def report(error: Exception) -> None:
        log.warning("Agent 365 telemetry: %s", error)

    try:
        await refresh_observability_token(config, force=True)
    except Exception as exc:  # noqa: BLE001 - the loop below retries
        log.warning(
            "Agent 365 telemetry: first token fetch failed (%s); retrying.", type(exc).__name__
        )
    await run_observability_token_refresh(config, on_error=report)
