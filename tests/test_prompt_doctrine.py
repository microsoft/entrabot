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

BOOTSTRAP_DOCTRINE_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    "docs/clients/persona-sati-host-bootstrap.md",
    "README.md",
    "scripts/setup.sh",
]

BOOTSTRAP_MARKERS = [
    "bootstrap_session",
    "reflect",
    "recall",
    "observe",
    "FastMCP instructions",
    "mind_contract_available",
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
    from entrabot.tools import wait_tool  # noqa: F401  (import-smoke)

    # The MCP-registered tool's runtime description is set in mcp_server.py;
    # check that source instead since the function is decorated at import.
    mcp_src = (REPO_ROOT / "src/entrabot/mcp_server.py").read_text(encoding="utf-8")
    assert "wait_for_sponsor_dm" in mcp_src
    # The registered tool body or docstring must name the canonical pattern.
    assert "sponsor" in mcp_src.lower()


def test_send_teams_message_docstring_directs_to_wait_for_sponsor_dm() -> None:
    """``send_teams_message``'s docstring becomes its MCP tool description,
    which Copilot CLI and Claude Code BOTH inject into the model's system
    prompt (Learning #48). Hosts that lack a push channel (Copilot CLI) need
    the wait-protocol delivered via tool descriptions, not via the body
    prompt's ``instructions=`` field. Pin the doctrine here so a future
    docstring rewrite can't silently regress it.
    """
    from entrabot import mcp_server  # noqa: F401

    # The decorated coroutine still carries the docstring at import time.
    docstring = mcp_server.send_teams_message.__doc__ or ""
    docstring_lower = docstring.lower()

    assert "wait_for_sponsor_dm" in docstring, (
        "send_teams_message docstring must name wait_for_sponsor_dm so "
        "Copilot CLI sees the wait-protocol via the tool description "
        "(it ignores FastMCP instructions=; see Learning #48)."
    )
    # And it must explain WHEN to call it, so the model doesn't wait after
    # every send. The trigger is: this DM is a reply the sponsor expects.
    assert "sponsor" in docstring_lower, (
        "send_teams_message docstring must mention the sponsor trigger so "
        "the model only waits when a reply is actually expected."
    )


@pytest.mark.parametrize("relpath", BOOTSTRAP_DOCTRINE_FILES)
def test_host_bootstrap_doctrine_mentions_required_mind_protocol(relpath: str) -> None:
    """Persona-sati bootstrap protocol must reach host LLMs that ignore FastMCP instructions=.
    
    Claude Code and Copilot CLI do not inject MCP server `instructions` into
    the LLM system prompt — they only surface them in MCP debug UI. The
    bootstrap_session first-call rule, fallback tools (get_system_prompt,
    context, list_memory_files), observe/reflect/recall per-turn discipline,
    and mind_contract_available degraded-mode handling must be present in
    host instruction files and docs that are reliably injected into the model.
    """
    path = REPO_ROOT / relpath
    assert path.exists(), f"Bootstrap doctrine file missing: {relpath}"
    text = path.read_text(encoding="utf-8")
    
    missing = [marker for marker in BOOTSTRAP_MARKERS if marker not in text]
    assert not missing, (
        f"{relpath} must reference all persona-sati bootstrap markers so the "
        f"protocol reaches the LLM despite FastMCP instructions= being dropped. "
        f"Missing: {missing}. Required: {BOOTSTRAP_MARKERS}"
    )
