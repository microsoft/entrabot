"""Self-scheduling for the harness: recurring or one-shot prompts injected back into the
session when due. Public surface re-exported so ``entrabot.harness.scheduler`` is stable."""

from .manager import InjectFn, ScheduledTask, SelfScheduler
from .spec import ScheduleSpec, parse_schedule

__all__ = ["InjectFn", "ScheduleSpec", "ScheduledTask", "SelfScheduler", "parse_schedule"]
