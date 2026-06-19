"""Per-caller permission gating (port + extension of the .NET permission model).

The .NET harness had a single session-wide yolo/interactive switch. Here the gate is
*caller-aware*: the active Teams caller (resolved from the message being handled) is
matched against a policy that can allow/deny specific tool kinds, shell commands, MCP
tools, etc. This is the whole point of the ENTRABOT harness — deterministically route
Teams traffic and fine-tune what each caller is allowed to trigger.

Wired into the SDK via ``create_session(on_permission_request=...)``. The handler returns
``PermissionDecisionApproveOnce`` / ``PermissionDecisionReject``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from copilot.generated import rpc as _rpc

# (kind, identifier, human-readable description) for a permission request.
Described = Tuple[str, str, str]

# Returns the id/UPN of the caller whose message is currently driving the turn (or None).
CallerResolver = Callable[[], Optional[str]]

# Optional UI confirm hook: (title, message) -> approved?
ConfirmFn = Callable[[str, str], Awaitable[bool]]


def describe(request: Any) -> Described:
    """Map an SDK PermissionRequest to (kind, identifier, human text)."""
    kind = getattr(request, "kind", "") or request.__class__.__name__
    if kind == "shell" or hasattr(request, "full_command_text"):
        cmd = getattr(request, "full_command_text", "")
        return ("shell", cmd, f"run shell command: {cmd}")
    if kind == "write" or hasattr(request, "file_name"):
        path = getattr(request, "file_name", "")
        return ("write", path, f"write file: {path}")
    if kind == "read" or (hasattr(request, "path") and not hasattr(request, "url")):
        path = getattr(request, "path", "")
        return ("read", path, f"read file: {path}")
    if kind == "url" or hasattr(request, "url"):
        url = getattr(request, "url", "")
        return ("url", url, f"fetch url: {url}")
    if kind == "mcp" or (hasattr(request, "server_name") and hasattr(request, "tool_name")):
        ident = f"{getattr(request, 'server_name', '')}.{getattr(request, 'tool_name', '')}"
        return ("mcp", ident, f"call MCP tool: {ident}")
    if kind in ("customTool", "custom") or hasattr(request, "tool_name"):
        name = getattr(request, "tool_name", "")
        return ("custom", name, f"call tool: {name}")
    return (kind, "", f"permission: {kind}")


@dataclass
class CallerPolicy:
    # mode is the default when no allow/deny token matches.
    mode: str = "ask"  # "allow" | "deny" | "ask"
    allow: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)

    def decide(self, kind: str, identifier: str) -> str:
        """Return "allow" | "deny" | "ask" for this (kind, identifier)."""
        if _matches_any(self.deny, kind, identifier):
            return "deny"
        if _matches_any(self.allow, kind, identifier):
            return "allow"
        return self.mode


def _matches_any(tokens: List[str], kind: str, identifier: str) -> bool:
    for tok in tokens:
        if tok == kind or tok == "*":
            return True
        if ":" in tok:
            tkind, _, glob = tok.partition(":")
            if tkind in (kind, "*") and fnmatch.fnmatch(identifier, glob):
                return True
    return False


class PermissionPolicy:
    """Holds the default policy plus per-caller overrides (keyed by caller id/UPN)."""

    def __init__(self, default: CallerPolicy, callers: Dict[str, CallerPolicy]):
        self.default = default
        self.callers = callers

    @classmethod
    def from_config(cls, raw: Dict[str, Any]) -> "PermissionPolicy":
        def policy(d: Dict[str, Any]) -> CallerPolicy:
            return CallerPolicy(
                mode=d.get("mode", "ask"),
                allow=list(d.get("allow", [])),
                deny=list(d.get("deny", [])),
            )

        default = policy(raw.get("default", {"mode": "ask"}))
        callers = {k: policy(v) for k, v in (raw.get("callers") or {}).items()}
        return cls(default, callers)

    def for_caller(self, caller: Optional[str]) -> CallerPolicy:
        if caller and caller in self.callers:
            return self.callers[caller]
        # case-insensitive UPN match
        if caller:
            low = caller.lower()
            for k, v in self.callers.items():
                if k.lower() == low:
                    return v
        return self.default


def build_permission_handler(
    policy: PermissionPolicy,
    resolve_caller: CallerResolver,
    *,
    yolo: bool = False,
    confirm: Optional[ConfirmFn] = None,
):
    """Return an async ``on_permission_request`` handler that consults the policy.

    Only the *undecided* ("ask") case is affected by yolo/confirm; an explicit policy
    ``allow``/``deny`` is always authoritative (so ``--yolo`` skips prompts but can never
    blow past a caller the policy explicitly denies):
    - yolo: an "ask" resolves to allow (no prompt).
    - confirm provided: an "ask" prompts the human via the UI.
    - otherwise (autonomous, no UI): an "ask" is denied (fail-closed).
    """

    async def handler(request: Any):
        kind, identifier, text = describe(request)
        caller = resolve_caller()
        decision = policy.for_caller(caller).decide(kind, identifier)

        if decision == "ask":
            if yolo:
                decision = "allow"
            elif confirm is not None:
                who = f" (requested while handling {caller})" if caller else ""
                approved = await confirm("Permission", f"Allow the agent to {text}?{who}")
                decision = "allow" if approved else "deny"
            else:
                decision = "deny"

        if decision == "allow":
            return _rpc.PermissionDecisionApproveOnce()
        who = caller or "this caller"
        return _rpc.PermissionDecisionReject(
            feedback=f"Denied by ENTRABOT permission policy: {who} may not {text}."
        )

    return handler
