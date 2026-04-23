"""Tests for informational host detection.

The EntraClaw MCP server records the connected client's ``clientInfo.name``
for logging purposes only. There is no leader/slave gating — every client
that spawns entraclaw (stdio) gets its own process with its own poll loops,
and channel pushes fire unconditionally. Clients that don't handle
``notifications/claude/channel`` drop them silently per the MCP spec.

This file covers:

- ``_current_host()`` — reads the active FastMCP context, returns the
  client-info name lowercased, and falls back to ``"unknown"`` before
  session initialize completes.
- ``_capture_host_from_context()`` — caches the live host into
  ``_state["cached_host"]`` so log lines from detached tasks can
  annotate with the most recently seen client.
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

    def test_returns_lowercased_when_client_is_capitalized(self) -> None:
        """Case-insensitive normalization."""
        from entraclaw import mcp_server

        fake_ctx = MagicMock()
        fake_ctx.session.client_params.clientInfo.name = "Claude Code"

        with patch.object(mcp_server.mcp, "get_context", return_value=fake_ctx):
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
# _capture_host_from_context
# ---------------------------------------------------------------------------
class TestCaptureHostFromContext:
    """``_capture_host_from_context()`` reads the live request context and
    updates ``_state['cached_host']`` so log annotations from detached
    background tasks can still include the most recent client name.

    The cache is a no-op when there's no live context, preserving any value
    set by a prior request.
    """

    def test_captures_live_host_into_cache(self) -> None:
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = ""
        try:
            with patch.object(
                mcp_server, "_current_host", return_value="claude-code"
            ):
                mcp_server._capture_host_from_context()
            assert mcp_server._state["cached_host"] == "claude-code"
        finally:
            mcp_server._state["cached_host"] = prior

    def test_no_live_context_preserves_existing_cache(self) -> None:
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "claude-code"
        try:
            with patch.object(mcp_server, "_current_host", return_value="unknown"):
                mcp_server._capture_host_from_context()
            assert mcp_server._state["cached_host"] == "claude-code"
        finally:
            mcp_server._state["cached_host"] = prior

    def test_empty_live_host_preserves_existing_cache(self) -> None:
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "claude-code"
        try:
            with patch.object(mcp_server, "_current_host", return_value=""):
                mcp_server._capture_host_from_context()
            assert mcp_server._state["cached_host"] == "claude-code"
        finally:
            mcp_server._state["cached_host"] = prior

    def test_new_host_overwrites_cache(self) -> None:
        """If a different client connects, the cache reflects that."""
        from entraclaw import mcp_server

        prior = mcp_server._state.get("cached_host", "")
        mcp_server._state["cached_host"] = "claude-code"
        try:
            with patch.object(
                mcp_server, "_current_host", return_value="github-copilot-cli"
            ):
                mcp_server._capture_host_from_context()
            assert mcp_server._state["cached_host"] == "github-copilot-cli"
        finally:
            mcp_server._state["cached_host"] = prior
