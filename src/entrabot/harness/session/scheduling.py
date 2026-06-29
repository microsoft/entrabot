"""Scheduling: the ``schedule_task``/``schedule_cancel`` agent tools, plus ``/schedules``
(list) and ``/watch`` (add a Teams chat to the poll set)."""

from __future__ import annotations

from typing import Any

import copilot
from pydantic import BaseModel, Field

from .. import config as cfgmod
from ..ui import UiStyle


class _SchedArgs(BaseModel):
    prompt: str = Field(description="The prompt to run when the schedule fires.")
    schedule: str = Field(
        description="e.g. 'every 30m', 'in 90s', 'daily at 09:00', '@hourly', or 5-field cron."
    )


class _CancelArgs(BaseModel):
    id: str = Field(description="The schedule id to cancel.")


class _SchedulingMixin:
    def _schedule_tools(self) -> list[Any]:
        async def _add(_ctx: Any, inv: copilot.ToolInvocation) -> str:
            arguments = inv.arguments
            prompt = arguments.get("prompt") if isinstance(arguments, dict) \
                else getattr(arguments, "prompt", "")
            schedule = arguments.get("schedule") if isinstance(arguments, dict) \
                else getattr(arguments, "schedule", "")
            try:
                task = self._scheduler.add(prompt, schedule)
            except ValueError as error:
                return f"error: {error}"
            return f"scheduled {task.id}: {schedule}"

        async def _cancel(_ctx: Any, inv: copilot.ToolInvocation) -> str:
            arguments = inv.arguments
            task_id = arguments.get("id") if isinstance(arguments, dict) \
                else getattr(arguments, "id", "")
            return "cancelled" if self._scheduler.cancel(task_id) else f"no such schedule {task_id}"

        return [
            copilot.define_tool(
                name="schedule_task",
                description="Schedule a prompt to run later (recurring or one-shot).",
                handler=_add,
                params_type=_SchedArgs,
                skip_permission=True,
            ),
            copilot.define_tool(
                name="schedule_cancel",
                description="Cancel a scheduled prompt by id.",
                handler=_cancel,
                params_type=_CancelArgs,
                skip_permission=True,
            ),
        ]

    def _handle_watch(self, args: list[str]) -> None:
        if not args:
            self._ui.append_line("usage: /watch <chat-id>", UiStyle.WARN)
            return
        chat = args[0]
        if self._bridge:
            self._bridge.watch(chat)
        if chat not in self._config.watched_chats:
            self._config.watched_chats.append(chat)
            cfgmod.save(self._root, self._config)
        self._ui.append_line(f"now watching {chat}", UiStyle.SUCCESS)

    def _print_schedules(self) -> None:
        tasks = self._scheduler.list() if self._scheduler else []
        if not tasks:
            self._ui.append_line("(no schedules)", UiStyle.DIM)
            return
        for task in tasks:
            self._ui.append_line(
                f"  {task.id}  {task.spec.raw}  → {task.next_due:%Y-%m-%d %H:%M}  {task.prompt[:50]}",
                UiStyle.INFO,
            )
