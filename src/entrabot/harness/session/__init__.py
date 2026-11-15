"""The interactive Copilot session. ``InteractiveSession`` is composed from one mixin per
concern (events, commands, model/mcp/permissions panels, sponsors, scheduling); re-exported
here so ``entrabot.harness.session.InteractiveSession`` stays the stable import path."""

from .core import InteractiveSession

__all__ = ["InteractiveSession"]
