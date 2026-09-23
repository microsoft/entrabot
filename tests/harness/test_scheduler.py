from datetime import datetime

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


@pytest.mark.parametrize("spec", ["@hourly", "@daily", "daily at 09:00", "weekdays at 17:30", "*/5 * * * *"])
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
