"""Tests for host detection and leader/slave mode gating.

The EntraClaw MCP server supports multiple MCP clients, but it was originally
built around Claude Code, which exposes a custom ``notifications/claude/channel``
for pushing inbound events to the model mid-turn. Hosts without a channel
mechanism (like GitHub Copilot CLI) would double-poll Teams and silently drop
every push notification if they ran the same background loops.

**Design decision (Brandon, 2026-04-20):** Claude Code is always the leader;
all other MCP hosts run in slave mode. Static designation based on the
``clientInfo.name`` the host sends at session initialize — not dynamic
election. Slaves run ZERO background tasks and get a per-response disclosure
on tools that expect an asynchronous reply (e.g. ``send_teams_message``).

This file covers:

- ``_current_host()`` — reads the active FastMCP context, returns the
  client-info name normalized to a well-known set, and falls back to
  ``"unknown"`` before session initialize completes.
- ``_is_leader_host()`` — convenience predicate for gating background
  task spawning and channel pushes.
- ``_slave_disclosure_suffix()`` — returns the disclosure string when the
  current host is NOT a leader, empty string otherwise.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _current_host
# ---------------------------------------------------------------------------
class TestCurrentHost:
    """``_current_host()`` returns the lowercased client name or ``unknown``."""

    def test_returns_claude_code_when_client_is_claude_code(self) -> None:
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params.clientInfo.name = "claude-code"

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
            assert mcp_server._current_host() == "claude-code"

    def test_returns_claude_code_when_client_is_capitalized(self) -> None:
        """Case-insensitive: ``Claude Code`` and ``claude-code`` normalize."""
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params.clientInfo.name = "Claude Code"

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
            # Normalized to lowercase + hyphenated for the compare below.
            assert mcp_server._current_host() == "claude code"

    def test_returns_github_copilot_cli_when_client_is_copilot(self) -> None:
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params.clientInfo.name = "github-copilot-cli"

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
            assert mcp_server._current_host() == "github-copilot-cli"

    def test_returns_unknown_when_client_info_absent(self) -> None:
        """Before session initialize, client_params is None."""
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params = None

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
            assert mcp_server._current_host() == "unknown"

    def test_returns_unknown_when_get_context_raises(self) -> None:
        """Outside a request context, FastMCP.get_context() raises. Treat as unknown."""
        from entraclaw import mcp_server

        def boom():
            raise LookupError("no active request context")

        with patch.object(mcp_server.mcp, "get_context", side_effect=boom):
            assert mcp_server._current_host() == "unknown"


# ---------------------------------------------------------------------------
# _is_leader_host
# ---------------------------------------------------------------------------
class TestIsLeaderHost:
    """Canonical leader set: ``{"claude-code", "claude code"}``.

    Any other host (including ``"unknown"``) is a slave. Static designation —
    no dynamic election, no config switch.
    """

    def test_claude_code_is_leader(self) -> None:
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_current_host", return_value="claude-code"):
            assert mcp_server._is_leader_host() is True

    def test_claude_code_with_space_is_leader(self) -> None:
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_current_host", return_value="claude code"):
            assert mcp_server._is_leader_host() is True

    def test_copilot_cli_is_not_leader(self) -> None:
        from entraclaw import mcp_server

        with patch.object(
            mcp_server, "_current_host", return_value="github-copilot-cli"
        ):
            assert mcp_server._is_leader_host() is False

    def test_unknown_is_not_leader(self) -> None:
        """Pre-initialize, default to slave. Safer: no accidental double-polling."""
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_current_host", return_value="unknown"):
            assert mcp_server._is_leader_host() is False


# ---------------------------------------------------------------------------
# _slave_disclosure_suffix
# ---------------------------------------------------------------------------
class TestSlaveDisclosureSuffix:
    """Returns disclosure text in slave mode, empty string in leader mode."""

    def test_leader_returns_empty_string(self) -> None:
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_is_leader_host", return_value=True):
            assert mcp_server._slave_disclosure_suffix() == ""

    def test_slave_returns_disclosure(self) -> None:
        from entraclaw import mcp_server

        with patch.object(mcp_server, "_is_leader_host", return_value=False):
            suffix = mcp_server._slave_disclosure_suffix()
            assert suffix  # non-empty
            assert "Reply channel unavailable" in suffix
            assert "Claude Code" in suffix
