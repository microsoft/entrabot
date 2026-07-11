"""Tool-name shape recognizer for outbound (write-shaped) MCP tools.

Per ``docs/architecture/PLAN-xpia-content-wrapping.md`` §"Deny-list guard
on outbound tool names": a lightweight regex that identifies write-shaped
tool names so future tools default to gated behavior even before their
explicit gate is written. Read this as a safety net, not the primary
enforcement — every write path also has its own audit + authorization
gates. The point of this recognizer is that a new tool named
``send_confidential_report`` inherits the "please audit before you ship
this" pressure before anyone reviews the tool.

Design note: the recognizer is a name-based deny-list rather than a
read-based allowlist because a deny-list is safer for evolution — a new
tool that we forgot to explicitly gate defaults to *more* gating, not
less. See the plan for the full trade-off.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("entrabot.tools.dispatch")

# Write-shaped tool names match this pattern. The plan spec originally
# restricted ``add_`` to ``add_(?:member|comment)`` verbatim, but this
# repo's actual write-shaped ``add_*`` tools are named
# ``add_teams_member``, ``add_file_comment``, ``add_word_comment``, and
# ``add_promise`` — none of which match the narrow variant. Broadening
# to ``add_`` catches all of them and keeps the "new write-shaped tool
# defaults to gated" invariant intact. False positives (e.g. a
# hypothetical ``add_note_to_local_cache`` that isn't actually a
# tenant-visible write) just get extra gating, which is the safe
# direction to fail.
_WRITE_SHAPED_PREFIX_RE = re.compile(
    r"^(send|reply|create|delete|upload|share|add_|resolve_)",
)


def is_write_shaped_tool_name(name: str) -> bool:
    """Return True when ``name`` matches the write-shaped prefix pattern.

    Behavior is pure and pattern-locked — callers that want to log or
    audit registration of a new write-shaped tool should do so at their
    call site so the log message carries their context.
    """
    if not name:
        return False
    return _WRITE_SHAPED_PREFIX_RE.search(name) is not None


def log_registration_if_write_shaped(name: str) -> None:
    """Emit a debug log when a tool named ``name`` is write-shaped.

    Called from tool-registration paths so a new write-shaped tool
    surfaces in server startup logs. No behavior change — purely
    observational. See the plan for the future-proofing rationale.
    """
    if is_write_shaped_tool_name(name):
        logger.debug(
            "dispatch: registered write-shaped tool %r (matches deny-list "
            "prefix pattern; future gating layers will target this shape)",
            name,
        )


__all__ = [
    "is_write_shaped_tool_name",
    "log_registration_if_write_shaped",
]
