from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from entrabot.config import EntraBotConfig
from entrabot.harness.config import HarnessConfig
from entrabot.harness.session import InteractiveSession, core, status
from entrabot.harness.ui import UI, UiStyle
from entrabot.observability import tokens


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> InteractiveSession:
    monkeypatch.setattr(core, "get_config", lambda: EntraBotConfig(
        a365_observability_enabled=True, a365_export_enabled=True,
        agent_id="agent-id", tenant_id="tenant-id",
    ))
    session = InteractiveSession(
        HarnessConfig(name="fixture-agent", description="Test agent", agent_id="session-id"),
        ".", Mock(spec=UI),
    )
    client = Mock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(core.copilot, "CopilotClient", Mock(return_value=client))
    monkeypatch.setattr(session, "_build_tools", lambda: [])
    monkeypatch.setattr(session, "_build_gate", Mock())
    monkeypatch.setattr(core.mcp_loader, "load", Mock(return_value={}))
    monkeypatch.setattr(
        session, "_establish", AsyncMock(return_value=Mock(disconnect=AsyncMock()))
    )
    monkeypatch.setattr(session, "_discover_slash_commands", AsyncMock())
    monkeypatch.setattr(core.toolcatalog, "enumerate_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(core, "SelfScheduler", Mock(return_value=Mock(stop=AsyncMock())))
    monkeypatch.setattr(session, "_announce_startup", Mock())
    return session


async def test_start_owns_one_refresh_task_and_dispose_cancels_and_awaits_it(
    monkeypatch: pytest.MonkeyPatch, session: InteractiveSession,
) -> None:
    entered = asyncio.Event()
    disposed = asyncio.Event()
    warnings: list[Exception] = []

    async def refresh(config: EntraBotConfig, *, on_error) -> None:
        assert config is session._observability_config
        on_error(ValueError("safe refresh warning"))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            disposed.set()

    monkeypatch.setattr(tokens, "run_observability_token_refresh", refresh, raising=False)
    monkeypatch.setattr(session, "_warn_observability", warnings.append)
    assert session._observability_refresh_task is None
    await session._start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    task = session._observability_refresh_task
    assert task is not None and not task.done()
    assert [str(error) for error in warnings] == ["safe refresh warning"]

    await session._dispose()
    await session._dispose()

    assert disposed.is_set()
    assert task.cancelled()
    assert session._observability_refresh_task is None


@pytest.mark.parametrize("failure", ["start", "establish"])
@pytest.mark.parametrize("via_run", [False, True])
async def test_failed_startup_disposes_refresh_even_when_ui_catches_error(
    monkeypatch: pytest.MonkeyPatch, session: InteractiveSession,
    failure: str, via_run: bool,
) -> None:
    tasks: list[asyncio.Task] = []
    exited = asyncio.Event()

    async def refresh(config: EntraBotConfig, *, on_error) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            exited.set()

    async def fail(*args, **kwargs) -> None:
        await asyncio.sleep(0)
        tasks.append(session._observability_refresh_task)
        raise RuntimeError("startup failed")

    monkeypatch.setattr(tokens, "run_observability_token_refresh", refresh, raising=False)
    if failure == "start":
        core.copilot.CopilotClient.return_value.start.side_effect = fail
    else:
        monkeypatch.setattr(session, "_establish", fail)
    client = core.copilot.CopilotClient.return_value

    async def run_ui(on_submit, on_interrupt, on_start) -> None:
        with pytest.raises(RuntimeError, match="startup failed"):
            await on_start()
        # The TUI catches startup exceptions and stays mounted; cleanup cannot wait for UI exit.
        assert exited.is_set()
        assert session._observability_refresh_task is None

    if via_run:
        session._ui.run.side_effect = run_ui
        await session.run()
    else:
        with pytest.raises(RuntimeError, match="startup failed"):
            await session._start()
    assert exited.is_set()
    assert tasks[0].cancelled()
    assert session._observability_refresh_task is None
    client.stop.assert_awaited_once()


async def test_run_failure_after_startup_disposes_refresh(
    monkeypatch: pytest.MonkeyPatch, session: InteractiveSession,
) -> None:
    tasks: list[asyncio.Task] = []

    async def refresh(config: EntraBotConfig, *, on_error) -> None:
        await asyncio.Event().wait()

    async def run_ui(on_submit, on_interrupt, on_start) -> None:
        await on_start()
        await asyncio.sleep(0)
        tasks.append(session._observability_refresh_task)
        raise RuntimeError("UI failed")

    monkeypatch.setattr(tokens, "run_observability_token_refresh", refresh, raising=False)
    session._ui.run.side_effect = run_ui
    with pytest.raises(RuntimeError, match="UI failed"):
        await session.run()

    assert tasks[0].cancelled()
    assert session._observability_refresh_task is None


@pytest.mark.parametrize(
    ("observability_enabled", "export_enabled"),
    [(False, False), (False, True), (True, False)],
)
async def test_disabled_session_never_starts_refresh_task(
    monkeypatch: pytest.MonkeyPatch, session: InteractiveSession,
    observability_enabled: bool, export_enabled: bool,
) -> None:
    session._observability_config = EntraBotConfig(
        a365_observability_enabled=observability_enabled,
        a365_export_enabled=export_enabled,
    )
    refresher = AsyncMock()
    monkeypatch.setattr(tokens, "run_observability_token_refresh", refresher, raising=False)
    await session._start()
    await asyncio.sleep(0)

    assert session._observability_refresh_task is None
    refresher.assert_not_called()
    await session._dispose()


async def test_refresh_auth_error_is_warned_without_response_secrets(
    monkeypatch: pytest.MonkeyPatch, session: InteractiveSession,
) -> None:
    from entrabot.errors import TokenExchangeError

    async def fail(*args, **kwargs) -> None:
        raise TokenExchangeError("hop3", "invalid_grant", "sensitive-auth-response")

    monkeypatch.setattr(tokens, "_TOKEN_CACHE", tokens.ObservabilityTokenCache())
    monkeypatch.setattr(tokens, "refresh_observability_token", fail)
    await session._start()
    for _ in range(3):
        await asyncio.sleep(0)
    await session._dispose()

    calls = session._ui.append_line.call_args_list
    assert any(
        "TokenExchangeError" in call.args[0] and call.args[1] == UiStyle.WARN
        for call in calls
    )
    assert "sensitive-auth-response" not in str(calls)


@pytest.mark.parametrize("during_startup", [False, True])
async def test_cancelling_run_disposes_pending_refresh(
    monkeypatch: pytest.MonkeyPatch, session: InteractiveSession, during_startup: bool,
) -> None:
    entered = asyncio.Event()
    refresh_exited = asyncio.Event()
    tasks: list[asyncio.Task] = []

    async def refresh(config: EntraBotConfig, *, on_error) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            refresh_exited.set()

    async def park(*args, **kwargs) -> None:
        await asyncio.sleep(0)
        tasks.append(session._observability_refresh_task)
        entered.set()
        await asyncio.Event().wait()

    async def run_ui(on_submit, on_interrupt, on_start) -> None:
        await on_start()
        await park()

    monkeypatch.setattr(tokens, "run_observability_token_refresh", refresh)
    session._ui.run.side_effect = run_ui
    if during_startup:
        core.copilot.CopilotClient.return_value.start.side_effect = park
    run_task = asyncio.create_task(session.run())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    assert tasks[0].cancelled()
    assert refresh_exited.is_set()
    assert session._observability_refresh_task is None
    core.copilot.CopilotClient.return_value.stop.assert_awaited_once()


async def test_disposal_cleans_resources_even_when_refresh_task_failed_unexpectedly(
    monkeypatch: pytest.MonkeyPatch, session: InteractiveSession,
) -> None:
    async def fail(config: EntraBotConfig, *, on_error) -> None:
        raise TypeError("unexpected bug")

    monkeypatch.setattr(tokens, "run_observability_token_refresh", fail)
    await session._start()
    await asyncio.sleep(0)
    scheduler = session._scheduler
    copilot_session = session._session
    client = session._client

    with pytest.raises(TypeError, match="unexpected bug"):
        await session._dispose()

    scheduler.stop.assert_awaited_once()
    copilot_session.disconnect.assert_awaited_once()
    client.stop.assert_awaited_once()
    assert session._observability_refresh_task is None


@pytest.mark.parametrize("replacement_fails", [False, True])
async def test_reload_releases_old_session_and_invocation_but_keeps_background_owners(
    monkeypatch, session, replacement_fails,
):
    old = Mock(abort=AsyncMock(), disconnect=AsyncMock())
    replacement = Mock(abort=AsyncMock(), disconnect=AsyncMock())
    session._session = old
    session._unsubscribe_session = unsubscribe = Mock()
    session._idle.clear()
    session._invocation_scope = invocation = object()
    session._scheduler = scheduler = Mock(stop=AsyncMock())
    session._observability_refresh_task = refresher = object()
    session._bridge = bridge = Mock(stop=AsyncMock())
    finish = Mock()
    monkeypatch.setattr(core, "finish_invocation", finish)
    monkeypatch.setattr(core, "record_invocation_error", Mock())
    monkeypatch.setattr(status.mcp_loader, "load", Mock(return_value={}))
    monkeypatch.setattr(status.toolcatalog, "enumerate_tools", AsyncMock(return_value=[]))

    async def establish(*args):
        unsubscribe.assert_called_once()
        old.abort.assert_awaited_once()
        old.disconnect.assert_awaited_once()
        finish.assert_called_once_with(invocation)
        assert session._invocation_scope is None
        if replacement_fails:
            raise RuntimeError("replacement failed")
        return replacement

    monkeypatch.setattr(session, "_establish", establish)
    if replacement_fails:
        with pytest.raises(RuntimeError, match="replacement failed"):
            await session._handle_reload()
        assert session._session is None
    else:
        await session._handle_reload()
        assert session._session is replacement
        monkeypatch.setattr(session, "_establish", AsyncMock(return_value=Mock()))
        await session._handle_reload()
        replacement.disconnect.assert_awaited_once()
        finish.assert_called_once_with(invocation)
    assert session._scheduler is scheduler
    assert session._observability_refresh_task is refresher
    assert session._bridge is bridge
    scheduler.stop.assert_not_called()
    bridge.stop.assert_not_called()


@pytest.mark.parametrize("concurrent", [False, True])
async def test_start_called_twice_does_not_create_multiple_clients_or_refresh_tasks(
    monkeypatch, session, concurrent,
):
    monkeypatch.setattr(
        tokens, "run_observability_token_refresh", AsyncMock(return_value=None),
    )
    try:
        if concurrent:
            await asyncio.gather(session._start(), session._start())
        else:
            await session._start()
        first = session._client
        task = session._observability_refresh_task
        await session._start()
        core.copilot.CopilotClient.assert_called_once()
        assert session._client is first
        assert session._observability_refresh_task is task
    finally:
        await session._dispose()
