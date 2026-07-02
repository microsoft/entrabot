#!/usr/bin/env python3
"""Claude Code PostToolUse hook: report send_teams_message success/failure honestly.

Why this exists
---------------

The previous PostToolUse hook for ``mcp__entrabot__send_teams_message`` was a
static ``echo`` that ALWAYS injected "Teams message sent." as additionalContext —
regardless of the tool result. But ``send_teams_message`` can *fail* and return an
error payload instead of sending, e.g. the placeholder-discipline gate returns::

    {"error": "...", "error_type": "MissingPlaceholderError", "remediation": "..."}

The static echo reported that failed call as a success, which is misleading: the
model (and anyone reading the transcript) is told the message went out when it did
not.

This hook inspects the PostToolUse ``tool_response`` and only reports "sent" when
the result carries no error; on an error result it says the message was NOT sent
and how to recover. It fails open (exit 0, no error verdict) on any malformed or
unexpected input so it can never block the tool pipeline.

Exit code: always 0 (advisory hook — it only emits additionalContext).
"""

from __future__ import annotations

import contextlib
import json
import sys

_SENT = (
    "Teams message sent. The background channel will push any replies "
    "automatically — no need to call watch_teams_replies unless you want to "
    "block and wait."
)

_NOT_SENT = (
    "Teams message was NOT sent — the tool returned an error (see the result "
    "above); do not report it as delivered. If it is a MissingPlaceholderError, "
    "call post_thinking_placeholder first (or use a short <=200-char message) "
    "and retry."
)


def _is_error_result(node: object) -> bool:
    """Return True if *node* (a PostToolUse tool_response) represents an error.

    Robust to the several shapes a tool_response can take: a dict, a JSON string,
    a ``{"result": "<json string>"}`` wrapper, or a list of content blocks. Any
    reachable dict with a truthy ``error`` or ``error_type`` key counts as an
    error; embedded JSON strings are parsed and inspected too.
    """
    stack: list[object] = [node]
    seen = 0
    while stack and seen < 10_000:  # bound the walk defensively
        seen += 1
        x = stack.pop()
        if isinstance(x, dict):
            if x.get("error") or x.get("error_type"):
                return True
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, str):
            s = x.strip()
            if s[:1] in ("{", "["):
                with contextlib.suppress(ValueError, TypeError):
                    stack.append(json.loads(s))
    return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, TypeError):
        return 0  # malformed payload — fail open, emit nothing

    context = _NOT_SENT if _is_error_result(payload.get("tool_response")) else _SENT
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
