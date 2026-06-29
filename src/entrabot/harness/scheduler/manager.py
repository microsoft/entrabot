"""``SelfScheduler`` — owns scheduled tasks, persists them to disk, and injects each
prompt back into the session when it comes due.

Persisted to ``<root>/.entrabot/harness.schedules.json``. Transport-agnostic: firing
calls an injected ``inject(prompt, caller_id, chat_id)`` coroutine.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from .spec import ScheduleSpec, parse_schedule

_TICK_SECONDS = 5
_SCHEDULES_FILE = os.path.join(".entrabot", "harness.schedules.json")

# (prompt, caller_id, chat_id) — scheduled prompts have no caller/chat (operator/system).
InjectFn = Callable[[str, str | None, str | None], Awaitable[None]]


@dataclass
class ScheduledTask:
    id: str
    prompt: str
    spec: ScheduleSpec
    next_due: datetime
    last_run: datetime | None = None

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
        self._tasks: dict[str, ScheduledTask] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._load()

    # ---- public API ------------------------------------------------------------------
    def add(self, prompt: str, schedule: str) -> ScheduledTask:
        spec = parse_schedule(schedule)
        due = spec.next_due(datetime.now())
        if due is None:
            raise ValueError(f"could not compute next run for {schedule!r}")
        task = ScheduledTask(id=uuid.uuid4().hex[:8], prompt=prompt, spec=spec, next_due=due)
        self._tasks[task.id] = task
        self._persist()
        return task

    def cancel(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        self._persist()
        return True

    def list(self) -> list[ScheduledTask]:
        return sorted(self._tasks.values(), key=lambda task: task.next_due)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()

    # ---- loop ------------------------------------------------------------------------
    async def _run(self) -> None:
        self._bump_overdue(datetime.now())
        while not self._stop.is_set():
            await asyncio.sleep(_TICK_SECONDS)
            now = datetime.now()
            for task in list(self._tasks.values()):
                if task.next_due <= now:
                    await self._fire(task, now)

    def _bump_overdue(self, now: datetime) -> None:
        """Tasks whose due time elapsed while the agent was offline fire promptly."""
        for task in self._tasks.values():
            if task.next_due < now:
                task.next_due = now

    async def _fire(self, task: ScheduledTask, now: datetime) -> None:
        task.last_run = now
        if not self._reschedule(task, now):
            self._tasks.pop(task.id, None)
        self._persist()
        try:
            await self._inject(_frame(task), None, None)
        except Exception:
            pass

    @staticmethod
    def _reschedule(task: ScheduledTask, now: datetime) -> bool:
        """Advance a recurring task to its next due time; return False to drop it (one-shot
        tasks and recurring tasks with no further occurrence)."""
        if task.spec.kind == "oneshot":
            return False
        next_run = task.spec.next_due(now)
        if next_run is None:
            return False
        task.next_due = next_run
        return True

    # ---- persistence -----------------------------------------------------------------
    def _path(self) -> str:
        return os.path.join(self._root, _SCHEDULES_FILE)

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self._path()), exist_ok=True)
        with open(self._path(), "w", encoding="utf-8") as handle:
            json.dump([task.to_json() for task in self._tasks.values()], handle, indent=2)

    def _load(self) -> None:
        try:
            with open(self._path(), encoding="utf-8") as handle:
                rows = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        for row in rows:
            task = self._task_from_row(row)
            if task is not None:
                self._tasks[task.id] = task

    @staticmethod
    def _task_from_row(row: dict) -> ScheduledTask | None:
        try:
            return ScheduledTask(
                id=row["id"],
                prompt=row["prompt"],
                spec=parse_schedule(row["schedule"]),
                next_due=datetime.fromisoformat(row["nextDue"]),
                last_run=datetime.fromisoformat(row["lastRun"]) if row.get("lastRun") else None,
            )
        except (KeyError, ValueError):
            return None


def _frame(task: ScheduledTask) -> str:
    return (
        f"[scheduled task {task.id}] This is a scheduled prompt firing now "
        f"(schedule: {task.spec.raw}). Do this now:\n\n{task.prompt}"
    )
