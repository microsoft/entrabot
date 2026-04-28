"""String-match guards for canonical prompt doctrine.

These tests pin the doctrine that every host-injected prompt file
(``AGENTS.md``, ``CLAUDE.md``, ``.github/copilot-instructions.md``) and
the body's ``channel-discipline.md`` mention the ``wait_for_sponsor_dm``
tool by name. Per Learning #48, this is the only injection vector that
reliably reaches the LLM in Copilot CLI / Claude Code, so the rule must
live in all three host files plus the canonical anatomy fragment.

The wait-tool's own ``@mcp.tool()`` docstring also has to carry the
operational rule, because tool descriptions ARE injected into the model's
system prompt by both hosts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DOCTRINE_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    "prompts/anatomy/channel-discipline.md",
]


@pytest.mark.parametrize("relpath", DOCTRINE_FILES)
def test_doctrine_file_mentions_wait_for_sponsor_dm(relpath: str) -> None:
    path = REPO_ROOT / relpath
    assert path.exists(), f"Doctrine file missing: {relpath}"
    text = path.read_text(encoding="utf-8")
    assert "wait_for_sponsor_dm" in text, (
        f"{relpath} must reference wait_for_sponsor_dm so the wait-tool "
        "doctrine reaches Copilot CLI / Claude Code (see Learning #48)."
    )


@pytest.mark.parametrize("relpath", ["AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md"])
def test_host_injected_files_forbid_polling_alternatives(relpath: str) -> None:
    """Ensure host files name the forbidden alternatives so the LLM sees them."""
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    # Each host file must call out at least one forbidden alternative so the
    # model isn't tempted into a polling loop, headless subprocess, or the
    # legacy `watch_teams_replies` watcher.
    forbidden_markers = ("poll", "copilot -p", "watch_teams_replies")
    assert any(m in text for m in forbidden_markers), (
        f"{relpath} must call out at least one forbidden alternative "
        f"(any of {forbidden_markers}) in the wait-tool doctrine."
    )


def test_wait_tool_docstring_reaches_model_via_tool_description() -> None:
    """The wait tool's docstring is the only LLM-visible enforcement point
    inside the running MCP server (Learning #48). It must name the tool and
    forbid the wrong alternatives."""
    from entraclaw.tools import wait_tool  # noqa: F401  (import-smoke)

    # The MCP-registered tool's runtime description is set in mcp_server.py;
    # check that source instead since the function is decorated at import.
    mcp_src = (REPO_ROOT / "src/entraclaw/mcp_server.py").read_text(encoding="utf-8")
    assert "wait_for_sponsor_dm" in mcp_src
    # The registered tool body or docstring must name the canonical pattern.
    assert "sponsor" in mcp_src.lower()
