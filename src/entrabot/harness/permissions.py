"""Per-caller-class, per-tool permission gating — the headline feature.

Every Teams caller is a **sponsor** (a configured human) or a **guest** (everyone else);
local terminal input is sponsor. Each individual tool (native / MCP / skill — see
:mod:`entrabot.harness.toolcatalog`) is independently enabled for sponsors and for guests.
The YOLO row is two independent toggles — ``sponsor_all`` / ``guest_all`` — that grant *all*
tools to that class.

Enforced via the SDK ``on_pre_tool_use`` hook, which fires for EVERY tool call (unlike
``on_permission_request``, which only fires for permission-gated tools). The hook gets a dict
with ``toolName`` and returns ``permissionDecision: "allow" | "deny"`` — deterministic, no prompts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Set

import copilot

# Returns the running turn's caller class: "sponsor" | "guest" (None -> local operator = sponsor).
ClassResolver = Callable[[], Optional[str]]


@dataclass
class ToolPolicy:
    sponsor_all: bool = True  # YOLO row, Sponsor column — sponsors get every tool
    guest_all: bool = False  # YOLO row, Guest column
    sponsor: Set[str] = field(default_factory=set)  # specific tool names for sponsors
    guest: Set[str] = field(default_factory=set)  # specific tool names for guests (default: none)

    @classmethod
    def from_config(cls, raw: Optional[Dict[str, Any]]) -> "ToolPolicy":
        if not raw:
            return cls()
        return cls(
            sponsor_all=bool(raw.get("sponsor_all", True)),
            guest_all=bool(raw.get("guest_all", False)),
            sponsor=set(raw.get("sponsor", [])),
            guest=set(raw.get("guest", [])),
        )

    def to_config(self) -> Dict[str, Any]:
        return {
            "sponsor_all": self.sponsor_all,
            "guest_all": self.guest_all,
            "sponsor": sorted(self.sponsor),
            "guest": sorted(self.guest),
        }

    def allowed(self, caller_class: str, tool: str) -> bool:
        if caller_class == "sponsor":
            return self.sponsor_all or tool in self.sponsor
        return self.guest_all or tool in self.guest


def _tool_name(inp: Any) -> Optional[str]:
    if isinstance(inp, dict):
        return inp.get("toolName") or inp.get("tool_name")
    return getattr(inp, "toolName", None) or getattr(inp, "tool_name", None)


def build_tool_gate(
    policy: ToolPolicy,
    resolve_class: ClassResolver,
    *,
    force_yolo: bool = False,
    always_allow: Optional[Set[str]] = None,
):
    """Return an ``on_pre_tool_use`` hook that allows/denies each tool by the running turn's
    caller class. Reads ``policy`` live, so /permissions edits apply without a reload.

    ``always_allow`` names are locked ON for every caller class — the harness's own reply-path
    tools (Teams send/read/list), which the agent needs to respond at all. They bypass the policy
    entirely (you can't deny the agent its own voice)."""
    locked = always_allow or set()

    async def hook(inp: Any, context: Any = None):
        name = _tool_name(inp)
        if not name:
            return None
        caller_class = resolve_class() or "sponsor"
        if name in locked or force_yolo or policy.allowed(caller_class, name):
            return copilot.PreToolUseHookOutput(permissionDecision="allow")
        return copilot.PreToolUseHookOutput(
            permissionDecision="deny",
            permissionDecisionReason=f"ENTRABOT policy: a {caller_class} may not use '{name}'.",
        )

    return hook
