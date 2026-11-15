"""Pure 5-field cron arithmetic: whether a datetime matches a cron expression, and
when the next matching minute is. No I/O, no harness imports — easy to unit-test."""

from __future__ import annotations

from datetime import datetime, timedelta

# Upper bound on the minute-by-minute search for the next cron match (~1 year). A valid
# recurring cron always matches well within this; the cap stops a malformed spec looping
# forever.
_CRON_SEARCH_LIMIT_MINUTES = 367 * 24 * 60


def _field_matches(field_spec: str, value: int, low: int, high: int) -> bool:
    """Does ``value`` satisfy a single cron field (``*``, ``a,b``, ``a-b``, ``*/step``)?"""
    if field_spec == "*":
        return True
    for part in field_spec.split(","):
        if "/" in part:
            base, _, step_text = part.partition("/")
            step = int(step_text)
            step_range = range(low, high + 1) if base in ("*", "") else _range_of(base, low, high)
            if value in (n for n in step_range if (n - step_range.start) % step == 0):
                return True
        elif "-" in part:
            range_start, _, range_end = part.partition("-")
            if int(range_start) <= value <= int(range_end):
                return True
        elif part.isdigit() and int(part) == value:
            return True
    return False


def _range_of(base: str, low: int, high: int) -> range:
    if "-" in base:
        range_start, _, range_end = base.partition("-")
        return range(int(range_start), int(range_end) + 1)
    return range(low, high + 1)


def _cron_matches(candidate: datetime, minute: str, hour: str, day_of_month: str,
                  month: str, day_of_week: str) -> bool:
    """Whether ``candidate`` satisfies all five cron fields (cron weekday: Sunday=0)."""
    cron_weekday = (candidate.weekday() + 1) % 7
    return (
        _field_matches(minute, candidate.minute, 0, 59)
        and _field_matches(hour, candidate.hour, 0, 23)
        and _field_matches(day_of_month, candidate.day, 1, 31)
        and _field_matches(month, candidate.month, 1, 12)
        and _field_matches(day_of_week, cron_weekday, 0, 6)
    )


def cron_next(cron: str, after: datetime) -> datetime | None:
    """First minute strictly after ``after`` that matches ``cron``, or None within ~1 year."""
    minute, hour, day_of_month, month, day_of_week = cron.split()
    candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(_CRON_SEARCH_LIMIT_MINUTES):
        if _cron_matches(candidate, minute, hour, day_of_month, month, day_of_week):
            return candidate
        candidate += timedelta(minutes=1)
    return None
