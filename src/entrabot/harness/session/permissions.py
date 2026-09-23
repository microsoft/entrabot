"""Per-caller-class, per-tool permission gating — the headline feature.

Every caller falls in one of three classes: **cli** (the local terminal operator), **sponsor**
(a configured/elevated human on Teams), or **guest** (everyone else). Each individual tool
(native / MCP / skill — see :mod:`entrabot.harness.session.toolcatalog`) is independently enabled per
class. The YOLO row is three independent toggles — ``cli_all`` / ``sponsor_all`` / ``guest_all``
— that grant *all* tools to that class.

Enforced via the SDK ``on_pre_tool_use`` hook, which fires for EVERY tool call (unlike
``on_permission_request``, which only fires for permission-gated tools). The hook gets a dict
with ``toolName`` and returns ``permissionDecision: "allow" | "deny"`` — deterministic, no prompts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import copilot

# Returns the running turn's caller class: "cli" | "sponsor" | "guest" (None -> defensive "cli").
ClassResolver = Callable[[], "str | None"]


@dataclass
class ToolPolicy:
    # YOLO row toggles — grant *all* tools to that caller class.
    cli_all: bool = True  # local terminal operator — fully trusted by default
    sponsor_all: bool = True  # configured sponsors get every tool
    guest_all: bool = False  # everyone else gets nothing
    # specific tool names per class
    cli: set[str] = field(default_factory=set)
    sponsor: set[str] = field(default_factory=set)
    guest: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> ToolPolicy:
        if not raw:
            return cls()
        return cls(
            cli_all=bool(raw.get("cli_all", True)),  # legacy config (no cli key) → operator trusted
            sponsor_all=bool(raw.get("sponsor_all", True)),
            guest_all=bool(raw.get("guest_all", False)),
            cli=set(raw.get("cli", [])),
            sponsor=set(raw.get("sponsor", [])),
            guest=set(raw.get("guest", [])),
        )

    def to_config(self) -> dict[str, Any]:
        return {
            "cli_all": self.cli_all,
            "sponsor_all": self.sponsor_all,
            "guest_all": self.guest_all,
            "cli": sorted(self.cli),
            "sponsor": sorted(self.sponsor),
            "guest": sorted(self.guest),
        }

    def allowed(self, caller_class: str, tool: str) -> bool:
        if caller_class == "cli":
            return self.cli_all or tool in self.cli
        if caller_class == "sponsor":
            return self.sponsor_all or tool in self.sponsor
        return self.guest_all or tool in self.guest


def _tool_name(hook_input: Any) -> str | None:
    if isinstance(hook_input, dict):
        return hook_input.get("toolName") or hook_input.get("tool_name")
    return getattr(hook_input, "toolName", None) or getattr(hook_input, "tool_name", None)


def build_tool_gate(
    policy: ToolPolicy,
    resolve_class: ClassResolver,
    *,
    force_yolo: bool = False,
    always_allow: set[str] | None = None,
):
    """Return an ``on_pre_tool_use`` hook that allows/denies each tool by the running turn's
    caller class. Reads ``policy`` live, so /permissions edits apply without a reload.

    ``always_allow`` names are locked ON for every caller class — the harness's own reply-path
    tools (Teams send/read/list), which the agent needs to respond at all. They bypass the policy
    entirely (you can't deny the agent its own voice)."""
    locked = always_allow or set()

    async def hook(hook_input: Any, context: Any = None):
        name = _tool_name(hook_input)
        if not name:
            return None
        caller_class = resolve_class() or "cli"  # no caller bound → local operator
        if name in locked or force_yolo or policy.allowed(caller_class, name):
            return copilot.PreToolUseHookOutput(permissionDecision="allow")
        return copilot.PreToolUseHookOutput(
            permissionDecision="deny",
            permissionDecisionReason=f"ENTRABOT policy: a {caller_class} may not use '{name}'.",
        )

    return hook
