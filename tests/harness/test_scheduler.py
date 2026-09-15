import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from entrabot.harness.scheduler import parse_schedule


def test_interval_and_oneshot():
    assert parse_schedule("every 30m").kind == "interval"
    assert parse_schedule("in 90s").kind == "oneshot"


def test_min_interval_rejected():
    with pytest.raises(ValueError):
        parse_schedule("every 5s")
    with pytest.raises(ValueError):
        parse_schedule("nonsense")


@pytest.mark.parametrize(
    "spec",
    ["@hourly", "@daily", "daily at 09:00", "weekdays at 17:30", "*/5 * * * *"],
)
def test_cron_forms_resolve(spec):
    sp = parse_schedule(spec)
    assert sp.kind == "cron"
    assert sp.next_due(datetime(2026, 1, 1, 8, 0)) is not None


def test_daily_at_next_due():
    sp = parse_schedule("daily at 09:00")
    nxt = sp.next_due(datetime(2026, 1, 1, 8, 0))
    assert nxt.hour == 9 and nxt.minute == 0


def test_cron_weekday_range():
    # Mondays-Fridays at 17:30; from a Saturday it should land on the next weekday
    sp = parse_schedule("weekdays at 17:30")
    nxt = sp.next_due(datetime(2026, 1, 3, 12, 0))  # 2026-01-03 is a Saturday
    assert nxt.weekday() < 5 and nxt.hour == 17 and nxt.minute == 30


@pytest.mark.parametrize("caller,chat,expected_class", [
    ("guest-user", "guest-chat", "guest"), (None, None, "cli"),
])
async def test_scheduled_tool_preserves_creator_permissions_after_reload(
    tmp_path, monkeypatch, caller, chat, expected_class,
):
    import copilot

    from entrabot.config import EntraBotConfig
    from entrabot.harness.config import HarnessConfig
    from entrabot.harness.scheduler import SelfScheduler
    from entrabot.harness.session import InteractiveSession, core

    monkeypatch.setattr(core, "get_config", EntraBotConfig)
    monkeypatch.setattr(copilot, "define_tool", lambda **kwargs: SimpleNamespace(**kwargs))
    runtime = InteractiveSession(HarnessConfig(name="fixture", description="fixture"),
                                 str(tmp_path), Mock())
    runtime._ctx.caller, runtime._ctx.chat = caller, chat
    runtime._session = Mock(send=AsyncMock())
    runtime._scheduler = SelfScheduler(str(tmp_path), runtime._inject)
    add_tool = next(tool for tool in runtime._schedule_tools() if tool.name == "schedule_task")
    await add_tool.handler(None, SimpleNamespace(arguments={
        "prompt": "use powershell", "schedule": "in 90s",
    }))

    loaded = SelfScheduler(str(tmp_path), runtime._inject)
    task = loaded.list()[0]
    runtime._ctx.caller = runtime._ctx.chat = None
    await loaded._fire(task, datetime.now())
    framed = runtime._session.send.call_args.args[0]
    runtime._on_event(SimpleNamespace(
        type=copilot.SessionEventType.USER_MESSAGE, data=SimpleNamespace(content=framed),
    ))

    assert runtime._caller_class() == expected_class
    assert runtime._ctx.chat == chat
    decision = await runtime._build_gate()({"toolName": "powershell", "toolArgs": {}})
    assert decision["permissionDecision"] == ("allow" if expected_class == "cli" else "deny")


async def test_legacy_schedule_without_creator_is_preserved_but_not_executed(tmp_path, caplog):
    from entrabot.harness.scheduler import SelfScheduler

    path = tmp_path / ".entrabot" / "harness.schedules.json"
    path.parent.mkdir()
    path.write_text(json.dumps([{
        "id": "legacy", "prompt": "fixture", "schedule": "every 1m",
        "nextDue": "2026-01-01T00:00:00",
    }]))
    inject = AsyncMock()
    scheduler = SelfScheduler(str(tmp_path), inject)
    await scheduler._fire(scheduler.list()[0], datetime.now())
    scheduler._persist()

    inject.assert_not_called()
    assert "creator" in caplog.text.lower()
    assert "callerId" not in json.loads(path.read_text())[0]


async def test_scheduler_stop_awaits_worker_cleanup(tmp_path, monkeypatch):
    from entrabot.harness.scheduler import SelfScheduler

    scheduler = SelfScheduler(str(tmp_path), AsyncMock())
    exited = asyncio.Event()
    entered = asyncio.Event()

    async def worker():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            exited.set()

    monkeypatch.setattr(scheduler, "_run", worker)
    scheduler.start()
    first = scheduler._task
    scheduler.start()
    assert scheduler._task is first
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        await scheduler.stop()
        assert exited.is_set()
        assert scheduler._task is None
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
