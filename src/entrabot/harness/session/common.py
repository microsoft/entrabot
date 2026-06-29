"""Constants shared across the session mixins.

Kept in a leaf module (it imports nothing from ``session``) so ``core`` and the mixins can
both use them without a core⇄mixin import cycle.
"""

from __future__ import annotations

from ..teams import TEAMS_TOOL_NAMES

# Harness reply-path tools — locked ON for every caller (the agent's own voice; never gated).
LOCKED_TOOLS = set(TEAMS_TOOL_NAMES)
