"""Parse a human schedule string into a :class:`ScheduleSpec`.

Supports ``every <dur>``, ``in <dur>``, ``daily at HH:MM``, ``weekdays at HH:MM``,
``@hourly/@daily/@weekly/@monthly``, and raw 5-field cron.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .cron import cron_next

_MIN_INTERVAL = timedelta(seconds=10)

_CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}


def _parse_duration(text: str) -> timedelta | None:
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    total = timedelta()
    digits = ""
    matched_any = False
    for char in text.strip().lower():
        if char.isdigit():
            digits += char
        elif char in units and digits:
            total += timedelta(**{units[char]: int(digits)})
            digits = ""
            matched_any = True
        else:
            return None
    if digits or not matched_any:
        return None
    return total


@dataclass
class ScheduleSpec:
    kind: str  # "interval" | "oneshot" | "cron"
    interval: timedelta | None = None
    cron: str | None = None
    raw: str = ""

    def next_due(self, after: datetime) -> datetime | None:
        if self.kind == "interval" and self.interval:
            return after + self.interval
        if self.kind == "oneshot" and self.interval is not None:
            return after + self.interval
        if self.kind == "cron" and self.cron:
            return cron_next(self.cron, after)
        return None


def parse_schedule(spec: str) -> ScheduleSpec:
    raw = spec.strip()
    lowered = raw.lower()
    if lowered in _CRON_ALIASES:
        return ScheduleSpec(kind="cron", cron=_CRON_ALIASES[lowered], raw=raw)
    if lowered.startswith("every "):
        duration = _parse_duration(lowered[len("every "):])
        if duration is None or duration < _MIN_INTERVAL:
            raise ValueError(f"invalid interval (min 10s): {spec!r}")
        return ScheduleSpec(kind="interval", interval=duration, raw=raw)
    if lowered.startswith("in "):
        duration = _parse_duration(lowered[len("in "):])
        if duration is None or duration < _MIN_INTERVAL:
            raise ValueError(f"invalid delay (min 10s): {spec!r}")
        return ScheduleSpec(kind="oneshot", interval=duration, raw=raw)
    if lowered.startswith("daily at "):
        return ScheduleSpec(kind="cron", cron=_hhmm_to_cron(lowered[len("daily at "):], "*"), raw=raw)
    if lowered.startswith("weekdays at "):
        return ScheduleSpec(
            kind="cron", cron=_hhmm_to_cron(lowered[len("weekdays at "):], "1-5"), raw=raw
        )
    # assume raw 5-field cron
    if len(raw.split()) == 5:
        return ScheduleSpec(kind="cron", cron=raw, raw=raw)
    raise ValueError(f"unrecognized schedule: {spec!r}")


def _hhmm_to_cron(text: str, day_of_week: str) -> str:
    hour_str, _, minute_str = text.strip().partition(":")
    return f"{int(minute_str)} {int(hour_str)} * * {day_of_week}"
