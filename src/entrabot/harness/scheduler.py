"""Self-scheduling (port of Session/Scheduling.cs + SelfScheduler.cs).

The agent can schedule recurring or one-shot prompts that are injected back into the
session when due. Supports ``every <dur>``, ``in <dur>``, ``daily at HH:MM``,
``weekdays at HH:MM``, ``@hourly/@daily/@weekly/@monthly``, and raw 5-field cron.

Persisted to ``<root>/.entrabot/harness.schedules.json``. Transport-agnostic: firing
calls an injected ``inject(prompt: str)`` coroutine.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional

_TICK_SECONDS = 5
_MIN_INTERVAL = timedelta(seconds=10)
_SCHEDULES_FILE = os.path.join(".entrabot", "harness.schedules.json")

InjectFn = Callable[[str], Awaitable[None]]


def _parse_duration(text: str) -> Optional[timedelta]:
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    total = timedelta()
    num = ""
    found = False
    for ch in text.strip().lower():
        if ch.isdigit():
            num += ch
        elif ch in units and num:
            total += timedelta(**{units[ch]: int(num)})
            num = ""
            found = True
        else:
            return None
    if num or not found:
        return None
    return total


@dataclass
class ScheduleSpec:
    kind: str  # "interval" | "oneshot" | "cron"
    interval: Optional[timedelta] = None
    cron: Optional[str] = None
    raw: str = ""

    def next_due(self, after: datetime) -> Optional[datetime]:
        if self.kind == "interval" and self.interval:
            return after + self.interval
        if self.kind == "oneshot" and self.interval is not None:
            return after + self.interval
        if self.kind == "cron" and self.cron:
            return _cron_next(self.cron, after)
        return None


_CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}


def parse_schedule(spec: str) -> ScheduleSpec:
    s = spec.strip()
    low = s.lower()
    if low in _CRON_ALIASES:
        return ScheduleSpec(kind="cron", cron=_CRON_ALIASES[low], raw=s)
    if low.startswith("every "):
        dur = _parse_duration(low[len("every "):])
        if dur is None or dur < _MIN_INTERVAL:
            raise ValueError(f"invalid interval (min 10s): {spec!r}")
        return ScheduleSpec(kind="interval", interval=dur, raw=s)
    if low.startswith("in "):
        dur = _parse_duration(low[len("in "):])
        if dur is None or dur < _MIN_INTERVAL:
            raise ValueError(f"invalid delay (min 10s): {spec!r}")
        return ScheduleSpec(kind="oneshot", interval=dur, raw=s)
    if low.startswith("daily at "):
        return ScheduleSpec(kind="cron", cron=_hhmm_to_cron(low[len("daily at "):], "*"), raw=s)
    if low.startswith("weekdays at "):
        return ScheduleSpec(kind="cron", cron=_hhmm_to_cron(low[len("weekdays at "):], "1-5"), raw=s)
    # assume raw 5-field cron
    if len(s.split()) == 5:
        return ScheduleSpec(kind="cron", cron=s, raw=s)
    raise ValueError(f"unrecognized schedule: {spec!r}")


def _hhmm_to_cron(text: str, dow: str) -> str:
    text = text.strip()
    hh, _, mm = text.partition(":")
    return f"{int(mm)} {int(hh)} * * {dow}"


def _field_matches(field_spec: str, value: int, lo: int, hi: int) -> bool:
    if field_spec == "*":
        return True
    for part in field_spec.split(","):
        if "/" in part:
            base, _, step = part.partition("/")
            step_i = int(step)
            rng = range(lo, hi + 1) if base in ("*", "") else _range_of(base, lo, hi)
            if value in (v for v in rng if (v - rng.start) % step_i == 0):
                return True
        elif "-" in part:
            a, _, b = part.partition("-")
            if int(a) <= value <= int(b):
                return True
        elif part.isdigit() and int(part) == value:
            return True
    return False


def _range_of(base: str, lo: int, hi: int) -> range:
    if "-" in base:
        a, _, b = base.partition("-")
        return range(int(a), int(b) + 1)
    return range(lo, hi + 1)


def _cron_next(cron: str, after: datetime) -> Optional[datetime]:
    minute, hour, dom, month, dow = cron.split()
    cand = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(367 * 24 * 60):  # search up to ~1 year of minutes
        wd = (cand.weekday() + 1) % 7  # cron: Sunday=0
        if (
            _field_matches(minute, cand.minute, 0, 59)
            and _field_matches(hour, cand.hour, 0, 23)
            and _field_matches(dom, cand.day, 1, 31)
            and _field_matches(month, cand.month, 1, 12)
            and _field_matches(dow, wd, 0, 6)
        ):
            return cand
        cand += timedelta(minutes=1)
    return None


@dataclass
class ScheduledTask:
    id: str
    prompt: str
    spec: ScheduleSpec
    next_due: datetime
    last_run: Optional[datetime] = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "schedule": self.spec.raw,
            "nextDue": self.next_due.isoformat(),
            "lastRun": self.last_run.isoformat() if self.last_run else None,
        }


class SelfScheduler:
    def __init__(self, root: str, inject: InjectFn):
        self._root = root
        self._inject = inject
        self._tasks: Dict[str, ScheduledTask] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._load()

    # ---- public API ------------------------------------------------------------------
    def add(self, prompt: str, schedule: str) -> ScheduledTask:
        spec = parse_schedule(schedule)
        now = datetime.now()
        due = spec.next_due(now)
        if due is None:
            raise ValueError(f"could not compute next run for {schedule!r}")
        task = ScheduledTask(id=uuid.uuid4().hex[:8], prompt=prompt, spec=spec, next_due=due)
        self._tasks[task.id] = task
        self._persist()
        return task

    def cancel(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._persist()
            return True
        return False

    def list(self) -> List[ScheduledTask]:
        return sorted(self._tasks.values(), key=lambda t: t.next_due)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    # ---- loop ------------------------------------------------------------------------
    async def _run(self) -> None:
        # Bump any tasks whose due time already elapsed (offline) so they fire promptly.
        now = datetime.now()
        for t in self._tasks.values():
            if t.next_due < now:
                t.next_due = now
        while not self._stop.is_set():
            await asyncio.sleep(_TICK_SECONDS)
            now = datetime.now()
            for task in list(self._tasks.values()):
                if task.next_due <= now:
                    task.last_run = now
                    if task.spec.kind == "oneshot":
                        self._tasks.pop(task.id, None)
                    else:
                        nxt = task.spec.next_due(now)
                        if nxt is None:
                            self._tasks.pop(task.id, None)
                        else:
                            task.next_due = nxt
                    self._persist()
                    try:
                        await self._inject(_frame(task))
                    except Exception:
                        pass

    # ---- persistence -----------------------------------------------------------------
    def _path(self) -> str:
        return os.path.join(self._root, _SCHEDULES_FILE)

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self._path()), exist_ok=True)
        with open(self._path(), "w", encoding="utf-8") as fh:
            json.dump([t.to_json() for t in self._tasks.values()], fh, indent=2)

    def _load(self) -> None:
        try:
            with open(self._path(), "r", encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        for row in rows:
            try:
                spec = parse_schedule(row["schedule"])
                self._tasks[row["id"]] = ScheduledTask(
                    id=row["id"],
                    prompt=row["prompt"],
                    spec=spec,
                    next_due=datetime.fromisoformat(row["nextDue"]),
                    last_run=datetime.fromisoformat(row["lastRun"]) if row.get("lastRun") else None,
                )
            except (KeyError, ValueError):
                continue


def _frame(task: ScheduledTask) -> str:
    return (
        f"[scheduled task {task.id}] This is a scheduled prompt firing now "
        f"(schedule: {task.spec.raw}). Do this now:\n\n{task.prompt}"
    )
