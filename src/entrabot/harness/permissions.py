"""Per-caller-class permission gating — the headline feature.

Every Teams caller is one of two classes: **sponsor** (a configured human who directs the
agent) or **guest** (everyone else). Each tool *category* can be independently enabled for
sponsors and for guests; guests default to nothing. A **yolo** flag (the matrix's top row, or
``--yolo``) allows everything for everyone. Local operator input (no Teams caller) is treated
as sponsor.

Wired into the SDK via ``create_session(on_permission_request=...)``: the handler resolves the
running turn's caller class and returns ApproveOnce / Reject deterministically (no prompts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from copilot.generated import rpc as _rpc

# The gateable capabilities, in display order. Keys match the kinds describe() emits.
TOOL_CATEGORIES: List[Tuple[str, str]] = [
    ("shell", "Run shell commands"),
    ("write", "Create / edit files"),
    ("read", "Read files"),
    ("url", "Fetch web URLs"),
    ("mcp", "Use MCP server tools"),
    ("custom", "Use custom tools"),
    ("memory", "Store memories"),
]
_KEYS = [k for k, _ in TOOL_CATEGORIES]

# (kind, identifier, human description) for a permission request.
Described = Tuple[str, str, str]
# Returns the active turn's caller class: "sponsor" | "guest" (or None for local operator).
ClassResolver = Callable[[], Optional[str]]


def describe(request: Any) -> Described:
    """Map an SDK PermissionRequest to (kind, identifier, human text)."""
    kind = getattr(request, "kind", "") or request.__class__.__name__
    if kind == "shell" or hasattr(request, "full_command_text"):
        cmd = getattr(request, "full_command_text", "")
        return ("shell", cmd, f"run shell command: {cmd}")
    if kind == "write" or hasattr(request, "file_name"):
        path = getattr(request, "file_name", "")
        return ("write", path, f"write file: {path}")
    if kind == "memory" or hasattr(request, "fact"):
        return ("memory", getattr(request, "fact", ""), "store a memory")
    if kind == "mcp" or (hasattr(request, "server_name") and hasattr(request, "tool_name")):
        ident = f"{getattr(request, 'server_name', '')}.{getattr(request, 'tool_name', '')}"
        return ("mcp", ident, f"call MCP tool: {ident}")
    if kind == "url" or hasattr(request, "url"):
        url = getattr(request, "url", "")
        return ("url", url, f"fetch url: {url}")
    if kind == "read" or (hasattr(request, "path") and not hasattr(request, "url")):
        path = getattr(request, "path", "")
        return ("read", path, f"read file: {path}")
    if kind in ("customTool", "custom") or hasattr(request, "tool_name"):
        name = getattr(request, "tool_name", "")
        return ("custom", name, f"call tool: {name}")
    return (kind, "", f"permission: {kind}")


@dataclass
class ToolPolicy:
    """yolo + the set of enabled capability keys for each caller class."""

    yolo: bool = False
    sponsor: Set[str] = field(default_factory=lambda: set(_KEYS))  # sponsors: all on by default
    guest: Set[str] = field(default_factory=set)  # guests: nothing by default

    @classmethod
    def from_config(cls, raw: Optional[Dict[str, Any]]) -> "ToolPolicy":
        if not raw:
            return cls()
        return cls(
            yolo=bool(raw.get("yolo", False)),
            sponsor=set(raw.get("sponsor", _KEYS)),
            guest=set(raw.get("guest", [])),
        )

    def to_config(self) -> Dict[str, Any]:
        return {"yolo": self.yolo, "sponsor": sorted(self.sponsor), "guest": sorted(self.guest)}

    def enabled_for(self, caller_class: str) -> Set[str]:
        return self.sponsor if caller_class == "sponsor" else self.guest

    def allowed(self, caller_class: str, kind: str) -> bool:
        return self.yolo or kind in self.enabled_for(caller_class)


def build_permission_handler(
    policy: ToolPolicy,
    resolve_class: ClassResolver,
    *,
    force_yolo: bool = False,
):
    """Return an async ``on_permission_request`` handler. Deterministic: allow if yolo (policy
    or ``--yolo``) or the caller class has the capability enabled; otherwise reject."""

    async def handler(request: Any, context: Any = None):
        kind, identifier, text = describe(request)
        caller_class = resolve_class() or "sponsor"  # local operator is trusted
        if force_yolo or policy.allowed(caller_class, kind):
            return _rpc.PermissionDecisionApproveOnce()
        return _rpc.PermissionDecisionReject(
            feedback=f"Denied by ENTRABOT policy: a {caller_class} may not {text}."
        )

    return handler
