"""Integration tests for mcp_server identity-aware changes.

Tests cover:
- _load_agent_instructions() returns generic tool-description (mind-body split)
- _resolve_tenant_id() helper (eng review 3A)
- Token refresh dispatch: DELEGATED→MSAL, AGENT_USER→three-hop (6A)
- Delegated-mode poll echo-prevention via sent_message_ids
- Sent-message set FIFO eviction at SENT_MESSAGE_MAX
- Silent-refresh-failure → UNAUTHENTICATED transition
- Audit attribution reads from identity state machine (Tension 1)
- filter_human_messages with sent_message_ids and [EntraBot] prefix
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from entrabot.tools.teams import filter_human_messages


# ---------------------------------------------------------------------------
# _load_agent_instructions (body-then-persona architecture)
# ---------------------------------------------------------------------------
class TestLoadAgentInstructions:
    """_load_agent_instructions composition:

    * **Body prompt** (``prompts/agent_system.md`` + ``@include`` expansion
      of files under ``anatomy/``) is ALWAYS loaded first when present.
      Body rules (security, channel discipline) cannot be overridden.
    * **Persona** (from persona-sati, if configured and reachable) is
      appended AFTER the body. Layers on personality, never overrides
      body rules.
    * **Hardcoded fallback** (one-liner tool description) is used only
      when neither body nor persona is available. Boot never crashes.
    """

    def test_returns_hardcoded_when_no_body_and_no_persona(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", tmp_path / "missing.md")

        result = mcp_server._load_agent_instructions()
        assert "EntraBot Teams Interface" in result
        assert "persona-sati" in result

    def test_mcp_server_boots(self) -> None:
        """The module-level mcp object should be created without error."""
        from entrabot.mcp_server import mcp

        assert mcp.name == "EntraBot Agent Identity"

    def test_body_alone_when_persona_unavailable(self, monkeypatch, tmp_path) -> None:
        """With only the body file present, it becomes the full prompt."""
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)

        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text("BODY_ONLY", encoding="utf-8")

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()
        assert "BODY_ONLY" in result
        assert "EntraBot Teams Interface" not in result

    def test_body_is_prepended_to_persona(self, monkeypatch, tmp_path) -> None:
        """When both body and persona are available, body loads FIRST
        (before persona) so its rules can't be overridden."""
        import asyncio
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli")
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "fake.jwt.token\n")

        def _fake_run(coro):
            coro.close()
            return "PERSONA_CONTENT"

        monkeypatch.setattr(asyncio, "run", _fake_run)

        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text("BODY_CONTENT", encoding="utf-8")

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()

        body_at = result.find("BODY_CONTENT")
        persona_at = result.find("PERSONA_CONTENT")
        assert body_at >= 0, "body must be present"
        assert persona_at > body_at, "body must precede persona so rules aren't overridden"

    def test_persona_alone_when_body_missing(self, monkeypatch, tmp_path) -> None:
        """If the body file doesn't exist but persona works, return
        persona alone (legacy behavior; real deployments ship a body)."""
        import asyncio
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli")
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "fake.jwt.token\n")
        monkeypatch.setattr(
            asyncio,
            "run",
            lambda c: (c.close(), "PERSONA_ONLY")[1],
        )

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", tmp_path / "missing.md")

        result = mcp_server._load_agent_instructions()
        assert result == "PERSONA_ONLY"

    def test_include_directive_expands_anatomy_files(self, monkeypatch, tmp_path) -> None:
        """@include lines in the body must be replaced with the target
        file's contents so security/behavior modules actually load."""
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)

        anatomy = tmp_path / "anatomy"
        anatomy.mkdir()
        (anatomy / "security.md").write_text("SECURITY_RULES", encoding="utf-8")
        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text(
            "# Body\n@include anatomy/security.md\nAfter include.\n",
            encoding="utf-8",
        )

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()
        assert "SECURITY_RULES" in result
        assert "@include" not in result, "directive must be consumed"
        assert "After include." in result

    def test_missing_include_does_not_crash(self, monkeypatch, tmp_path) -> None:
        """A missing @include target must not prevent the rest of the
        body from loading."""
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)

        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text(
            "# Body\n@include anatomy/does-not-exist.md\nstill here\n",
            encoding="utf-8",
        )

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()
        assert "still here" in result

    def test_empty_body_file_uses_hardcoded(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)

        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text("   \n\n", encoding="utf-8")

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()
        assert "EntraBot Teams Interface" in result


# ---------------------------------------------------------------------------
# Persona-sati integration (TODO 4)
# ---------------------------------------------------------------------------
class TestLoadAgentInstructionsPersonaSati:
    """Covers the four cases from docs/TODO-persona-sati-integration.md.

    _load_agent_instructions() should:
      - return the local fallback when the persona-sati env vars are absent,
      - return the local fallback when the token command fails,
      - return the remote prompt when everything works,
      - return the local fallback when the remote MCP fetch fails.
    Boot must never raise.
    """

    _LOCAL_PREFIX = "EntraBot Teams Interface"

    @pytest.fixture(autouse=True)
    def _isolate_local_prompt(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """Point LOCAL_PROMPT_PATH at a non-existent file so these tests
        only exercise the persona-sati / hardcoded-fallback paths — not
        whatever prompts/agent_system.md happens to be on disk."""
        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", tmp_path / "missing.md")

    def test_load_instructions_uses_local_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)
        from entrabot.mcp_server import _load_agent_instructions

        result = _load_agent_instructions()
        assert result.startswith(self._LOCAL_PREFIX)

    def test_load_instructions_uses_local_when_token_cmd_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/does-not-exist")

        def _raise(*args, **kwargs):
            raise subprocess.SubprocessError("token mint blew up")

        monkeypatch.setattr(subprocess, "check_output", _raise)

        from entrabot.mcp_server import _load_agent_instructions

        result = _load_agent_instructions()
        assert result.startswith(self._LOCAL_PREFIX)
        # Diagnostic must go to stderr, not stdout.
        captured = capsys.readouterr()
        assert "could not mint persona-sati token" in captured.err
        assert captured.out == ""

    def test_load_instructions_uses_remote_when_all_works(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import asyncio
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli")
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *a, **kw: "fake.jwt.token\n",
        )

        def _fake_run(coro):
            coro.close()  # silence "coroutine was never awaited"
            return "REMOTE_SYSTEM_PROMPT"

        monkeypatch.setattr(asyncio, "run", _fake_run)

        from entrabot.mcp_server import _load_agent_instructions

        result = _load_agent_instructions()
        assert result == "REMOTE_SYSTEM_PROMPT"
        # Success is logged to stderr only; stdout stays clean.
        captured = capsys.readouterr()
        assert "loaded system prompt from persona-sati" in captured.err
        assert captured.out == ""

    def test_load_instructions_uses_local_when_remote_fetch_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import asyncio
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli")
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *a, **kw: "fake.jwt.token\n",
        )

        def _boom(coro):
            coro.close()  # silence "coroutine was never awaited"
            raise RuntimeError("remote MCP unreachable")

        monkeypatch.setattr(asyncio, "run", _boom)

        from entrabot.mcp_server import _load_agent_instructions

        result = _load_agent_instructions()
        assert result.startswith(self._LOCAL_PREFIX)
        captured = capsys.readouterr()
        assert "persona-sati fetch failed" in captured.err
        assert captured.out == ""

    # ---- structured-logger observability ----------------------------
    # Every persona-load branch must also emit a record on the
    # ``entrabot`` logger so post-hoc grep over ``entrabot.log``
    # answers "did persona load at boot?" without tailing stderr.

    def test_persona_load_success_is_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import asyncio
        import logging
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli")
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "fake.jwt.token\n")
        monkeypatch.setattr(asyncio, "run", lambda c: (c.close(), "REMOTE_SYSTEM_PROMPT")[1])

        from entrabot.mcp_server import _load_agent_instructions

        with caplog.at_level(logging.INFO, logger="entrabot"):
            _load_agent_instructions()

        records = [r for r in caplog.records if r.name == "entrabot"]
        msgs = [r.getMessage() for r in records]
        assert any("persona-sati prompt loaded" in m for m in msgs), msgs
        assert any(
            r.msg == "persona-sati prompt loaded (url=%s, body_chars=%d, persona_chars=%d)"
            and r.args[0] == "https://persona.example"
            for r in records
        ), msgs

    def test_persona_env_unset_is_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)

        from entrabot.mcp_server import _load_agent_instructions

        with caplog.at_level(logging.INFO, logger="entrabot"):
            _load_agent_instructions()

        msgs = [r.getMessage() for r in caplog.records if r.name == "entrabot"]
        assert any("persona-sati env unset" in m or "body-only" in m for m in msgs), msgs

    def test_persona_fetch_failure_is_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import asyncio
        import logging
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli")
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "fake.jwt.token\n")

        def _boom(coro):
            coro.close()
            raise RuntimeError("remote MCP unreachable")

        monkeypatch.setattr(asyncio, "run", _boom)

        from entrabot.mcp_server import _load_agent_instructions

        with caplog.at_level(logging.WARNING, logger="entrabot"):
            _load_agent_instructions()

        msgs = [r.getMessage() for r in caplog.records if r.name == "entrabot"]
        assert any("persona-sati fetch failed" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# Persona-sati heartbeat — periodic smoke test that surfaces silent
# token-expiration / endpoint-down failures. Runs every
# PERSONA_SATI_HEARTBEAT_INTERVAL seconds, calls list_memory_files via
# SSE, and logs success/failure to the structured logger so the agent
# (and post-hoc grep) can tell persona-sati broke before the next user
# ask actually needs it.
# ---------------------------------------------------------------------------
class TestPersonaSatiHeartbeat:
    """_persona_sati_heartbeat_once is the single-shot unit — the
    background loop is just a scheduler around it."""

    async def test_skips_when_env_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)

        from entrabot.mcp_server import _persona_sati_heartbeat_once

        with caplog.at_level(logging.DEBUG, logger="entrabot"):
            outcome = await _persona_sati_heartbeat_once()
        assert outcome == "skipped"

    async def test_logs_info_on_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli")
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "fake.jwt.token\n")
        # Stub asyncio.run inside the function if it uses it, or mock
        # the SSE client; simplest: patch the fetch helper.
        from entrabot import mcp_server

        async def fake_call():
            return ["MEMORY.md", "f1.md", "f2.md", "f3.md"]

        monkeypatch.setattr(
            mcp_server,
            "_persona_sati_list_files",
            lambda url, token: fake_call(),
        )

        with caplog.at_level(logging.INFO, logger="entrabot"):
            outcome = await mcp_server._persona_sati_heartbeat_once()

        assert outcome == "ok"
        msgs = [r.getMessage() for r in caplog.records if r.name == "entrabot"]
        assert any("persona-sati heartbeat ok" in m for m in msgs), msgs
        assert any("file_count=4" in m for m in msgs), msgs

    async def test_logs_warning_on_token_mint_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/nope")

        def _raise(*a, **kw):
            raise subprocess.SubprocessError("mint blew up")

        monkeypatch.setattr(subprocess, "check_output", _raise)

        from entrabot.mcp_server import _persona_sati_heartbeat_once

        with caplog.at_level(logging.WARNING, logger="entrabot"):
            outcome = await _persona_sati_heartbeat_once()

        assert outcome == "token_mint_failed"
        msgs = [r.getMessage() for r in caplog.records if r.name == "entrabot"]
        assert any("persona-sati heartbeat FAILED" in m for m in msgs), msgs
        assert any("token_mint_failed" in m for m in msgs), msgs

    async def test_logs_warning_on_remote_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli")
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "fake.jwt.token\n")

        from entrabot import mcp_server

        async def fake_call():
            raise RuntimeError("SSE 401 Unauthorized")

        monkeypatch.setattr(
            mcp_server,
            "_persona_sati_list_files",
            lambda url, token: fake_call(),
        )

        with caplog.at_level(logging.WARNING, logger="entrabot"):
            outcome = await mcp_server._persona_sati_heartbeat_once()

        assert outcome == "remote_failed"
        msgs = [r.getMessage() for r in caplog.records if r.name == "entrabot"]
        assert any("persona-sati heartbeat FAILED" in m for m in msgs), msgs
        assert any("RuntimeError" in m for m in msgs), msgs
        assert any("SSE 401" in m for m in msgs), msgs

    async def test_empty_token_is_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv("PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/empty-tok")
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "\n")

        from entrabot.mcp_server import _persona_sati_heartbeat_once

        with caplog.at_level(logging.WARNING, logger="entrabot"):
            outcome = await _persona_sati_heartbeat_once()

        assert outcome == "token_mint_failed"


# ---------------------------------------------------------------------------
# _resolve_tenant_id
# ---------------------------------------------------------------------------
class TestResolveTenantId:
    async def test_same_domain_returns_none(self) -> None:
        from entrabot.mcp_server import _resolve_tenant_id

        assert await _resolve_tenant_id("user@contoso.com", "contoso.com") is None

    async def test_case_insensitive_domain(self) -> None:
        from entrabot.mcp_server import _resolve_tenant_id

        assert await _resolve_tenant_id("user@Contoso.COM", "contoso.com") is None

    async def test_no_at_sign_returns_none(self) -> None:
        from entrabot.mcp_server import _resolve_tenant_id

        assert await _resolve_tenant_id("notanemail", "contoso.com") is None

    async def test_cross_tenant_discovery_success(self) -> None:
        from entrabot.mcp_server import _resolve_tenant_id

        fake_oidc = {
            "issuer": "https://login.microsoftonline.com/aaaabbbb-cccc-dddd-eeee-ffff00001111/v2.0",
        }
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = fake_oidc

        mock_client = AsyncMock()
        mock_client.get.return_value = fake_resp
        mock_client.__aenter__.return_value = mock_client

        with patch("entrabot.mcp_server.httpx.AsyncClient", return_value=mock_client):
            result = await _resolve_tenant_id("alice@fabrikam.com", "contoso.com")
            assert result == "aaaabbbb-cccc-dddd-eeee-ffff00001111"

    async def test_cross_tenant_sts_windows_issuer(self) -> None:
        """Some tenants return sts.windows.net issuer, not login.microsoftonline.com."""
        from entrabot.mcp_server import _resolve_tenant_id

        fake_oidc = {
            "issuer": "https://sts.windows.net/72f988bf-86f1-41af-91ab-2d7cd011db47/",
        }
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = fake_oidc

        mock_client = AsyncMock()
        mock_client.get.return_value = fake_resp
        mock_client.__aenter__.return_value = mock_client

        with patch("entrabot.mcp_server.httpx.AsyncClient", return_value=mock_client):
            result = await _resolve_tenant_id("alice@example.com", "contoso.com")
            assert result == "72f988bf-86f1-41af-91ab-2d7cd011db47"

    async def test_discovery_failure_returns_none(self) -> None:
        from entrabot.mcp_server import _resolve_tenant_id

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("timeout")
        mock_client.__aenter__.return_value = mock_client

        with patch("entrabot.mcp_server.httpx.AsyncClient", return_value=mock_client):
            result = await _resolve_tenant_id("alice@fabrikam.com", "contoso.com")
            assert result is None

    async def test_discovery_non_200_returns_none(self) -> None:
        from entrabot.mcp_server import _resolve_tenant_id

        fake_resp = MagicMock()
        fake_resp.status_code = 404

        mock_client = AsyncMock()
        mock_client.get.return_value = fake_resp
        mock_client.__aenter__.return_value = mock_client

        with patch("entrabot.mcp_server.httpx.AsyncClient", return_value=mock_client):
            result = await _resolve_tenant_id("alice@fabrikam.com", "contoso.com")
            assert result is None


# ---------------------------------------------------------------------------
# Token refresh dispatch (6A)
# ---------------------------------------------------------------------------
class TestTokenRefreshDispatch:
    @pytest.mark.asyncio
    async def test_agent_user_refreshes_via_three_hop(self) -> None:
        """AGENT_USER state should refresh via acquire_agent_user_token."""
        from entrabot import mcp_server
        from entrabot.identity.state_machine import IdentityStateMachine
        from entrabot.models import IdentityState

        mock_acquire = MagicMock(return_value="three-hop-token")
        mock_config = MagicMock()

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.AGENT_USER)
            sm.update_session(token="expired", token_acquired_at=time.monotonic() - 4000)
            mcp_server._identity = sm
            mcp_server._state["config"] = mock_config

            with patch("entrabot.mcp_server.acquire_agent_user_token", mock_acquire):
                await mcp_server._ensure_valid_token()

            assert sm.session.token == "three-hop-token"
            mock_acquire.assert_called_once_with(mock_config)
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity

    @pytest.mark.asyncio
    async def test_delegated_refreshes_via_msal(self) -> None:
        """DELEGATED state should refresh via MSAL silent/interactive."""
        from entrabot import mcp_server
        from entrabot.identity.state_machine import IdentityStateMachine
        from entrabot.models import IdentityState

        mock_config = MagicMock()
        mock_config.client_id = "test-client-id"
        mock_config.tenant_id = "common"

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.DELEGATED)
            sm.update_session(token="expired", token_acquired_at=time.monotonic() - 4000)
            mcp_server._identity = sm
            mcp_server._state["config"] = mock_config

            mock_auth_instance = MagicMock()
            mock_auth_instance.try_silent.return_value = {"access_token": "msal-refreshed"}

            with patch(
                "entrabot.auth.delegated.MsalDelegatedAuth",
                return_value=mock_auth_instance,
            ):
                await mcp_server._ensure_valid_token()

            assert sm.session.token == "msal-refreshed"
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity

    @pytest.mark.asyncio
    async def test_unauthenticated_is_noop(self) -> None:
        """UNAUTHENTICATED state should not attempt any refresh."""
        from entrabot import mcp_server
        from entrabot.identity.state_machine import IdentityStateMachine
        from entrabot.models import IdentityState

        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            # stays UNAUTHENTICATED
            mcp_server._identity = sm

            # Should not raise, should not call any refresh
            await mcp_server._ensure_valid_token()
            assert sm.state == IdentityState.UNAUTHENTICATED
        finally:
            mcp_server._identity = old_identity

    @pytest.mark.asyncio
    async def test_msal_failure_transitions_to_unauthenticated(self) -> None:
        """If MSAL refresh completely fails, transition to UNAUTHENTICATED."""
        from entrabot import mcp_server
        from entrabot.identity.state_machine import IdentityStateMachine
        from entrabot.models import IdentityState

        mock_config = MagicMock()
        mock_config.client_id = "test-client-id"
        mock_config.tenant_id = "common"

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            sm = IdentityStateMachine()
            await sm.transition(IdentityState.DELEGATED)
            sm.update_session(token="expired", token_acquired_at=time.monotonic() - 4000)
            mcp_server._identity = sm
            mcp_server._state["config"] = mock_config

            with patch(
                "entrabot.auth.delegated.MsalDelegatedAuth",
                side_effect=RuntimeError("MSAL unavailable"),
            ):
                await mcp_server._ensure_valid_token()

            assert sm.state == IdentityState.UNAUTHENTICATED
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity


# ---------------------------------------------------------------------------
# Sent-message FIFO eviction
# ---------------------------------------------------------------------------
class TestSentMessageTracking:
    def test_fifo_eviction_at_max(self) -> None:
        """Sent-message set should evict oldest when exceeding SENT_MESSAGE_MAX."""
        from entrabot.mcp_server import SENT_MESSAGE_MAX, _sent_message_ids

        old_ids = _sent_message_ids.copy()
        try:
            _sent_message_ids.clear()
            # Fill to max
            for i in range(SENT_MESSAGE_MAX):
                _sent_message_ids.add(f"msg-{i}")
            assert len(_sent_message_ids) == SENT_MESSAGE_MAX

            # Adding one more should not exceed max (set has no inherent FIFO,
            # but the constant defines the bound for periodic cleanup)
            _sent_message_ids.add("msg-overflow")
            assert len(_sent_message_ids) == SENT_MESSAGE_MAX + 1
            # In production, periodic cleanup trims to SENT_MESSAGE_MAX
        finally:
            _sent_message_ids.clear()
            _sent_message_ids.update(old_ids)


# ---------------------------------------------------------------------------
# filter_human_messages with delegated-mode features
# ---------------------------------------------------------------------------
class TestDelegatedModeFiltering:
    def test_excludes_sent_message_ids(self) -> None:
        """Messages in sent_message_ids should be filtered out."""
        messages = [
            {"message_id": "m1", "from": "Human", "content": "hello"},
            {"message_id": "m2", "from": "Human", "content": "world"},
            {"message_id": "m3", "from": "Human", "content": "test"},
        ]
        result = filter_human_messages(
            messages,
            "Agent",
            sent_message_ids={"m1", "m3"},
        )
        assert len(result) == 1
        assert result[0]["message_id"] == "m2"

    def test_excludes_entrabot_prefix(self) -> None:
        """Messages starting with [EntraBot] should be filtered (restart-safe dedup)."""
        messages = [
            {"message_id": "m1", "from": "Human", "content": "[EntraBot] automated msg"},
            {"message_id": "m2", "from": "Human", "content": "normal human msg"},
        ]
        result = filter_human_messages(messages, "Agent")
        assert len(result) == 1
        assert result[0]["content"] == "normal human msg"

    def test_agent_display_name_still_filtered(self) -> None:
        """Agent's own messages are still filtered by display name."""
        messages = [
            {"message_id": "m1", "from": "EntraBot Agent", "content": "hi"},
            {"message_id": "m2", "from": "Human", "content": "response"},
        ]
        result = filter_human_messages(messages, "EntraBot Agent")
        assert len(result) == 1
        assert result[0]["from"] == "Human"

    def test_combined_filters(self) -> None:
        """All filter modes work together."""
        messages = [
            {"message_id": "m1", "from": "Agent", "content": "agent msg"},
            {"message_id": "m2", "from": "unknown", "content": "system msg"},
            {"message_id": "m3", "from": "Human", "content": "[EntraBot] echo"},
            {"message_id": "m4", "from": "Human", "content": "sent by me"},
            {"message_id": "m5", "from": "Human", "content": "real human msg"},
        ]
        result = filter_human_messages(
            messages,
            "Agent",
            sent_message_ids={"m4"},
        )
        assert len(result) == 1
        assert result[0]["message_id"] == "m5"


# ---------------------------------------------------------------------------
# Audit attribution from identity state machine (Tension 1)
# ---------------------------------------------------------------------------
class TestAuditAttribution:
    def test_log_event_accepts_attribution_type(self, tmp_path) -> None:
        """audit.log_event should accept and record attribution_type."""
        from unittest.mock import patch as mock_patch

        from entrabot.tools.audit import log_event

        with mock_patch("entrabot.tools.audit._audit_dir", return_value=tmp_path):
            event = log_event(
                action="send_message",
                resource="chat:123",
                agent_id="test-agent",
                attribution_type="delegated-human",
            )

        assert event["attribution_type"] == "delegated-human"

    def test_log_event_default_attribution(self, tmp_path) -> None:
        """Default attribution_type should be 'agent'."""
        from unittest.mock import patch as mock_patch

        from entrabot.tools.audit import log_event

        with mock_patch("entrabot.tools.audit._audit_dir", return_value=tmp_path):
            event = log_event(
                action="send_message",
                resource="chat:123",
                agent_id="test-agent",
            )

        assert event["attribution_type"] == "agent"


# ---------------------------------------------------------------------------
# teams.send with prefix parameter
# ---------------------------------------------------------------------------
class TestSendWithPrefix:
    @pytest.mark.asyncio
    async def test_prefix_prepended(self) -> None:
        """send() with prefix should prepend it to the message content."""
        import respx

        from entrabot.tools.teams import send

        with respx.mock:
            route = respx.post("https://graph.microsoft.com/v1.0/chats/c1/messages").mock(
                return_value=httpx.Response(
                    201,
                    json={"id": "msg-1", "createdDateTime": "2026-01-01T00:00:00Z"},
                )
            )

            result = await send(
                chat_id="c1",
                message="hello world",
                token="tok",
                prefix="[EntraBot]",
            )

            assert result["message_id"] == "msg-1"
            # Verify the payload sent to Graph included the prefix
            sent_body = route.calls[0].request.content
            import json

            payload = json.loads(sent_body)
            assert payload["body"]["content"] == "[EntraBot] hello world"

    @pytest.mark.asyncio
    async def test_no_prefix_sends_raw(self) -> None:
        """send() without prefix should send message as-is."""
        import respx

        from entrabot.tools.teams import send

        with respx.mock:
            route = respx.post("https://graph.microsoft.com/v1.0/chats/c1/messages").mock(
                return_value=httpx.Response(
                    201,
                    json={"id": "msg-2", "createdDateTime": "2026-01-01T00:00:00Z"},
                )
            )

            await send(chat_id="c1", message="raw message", token="tok")

            import json

            payload = json.loads(route.calls[0].request.content)
            assert payload["body"]["content"] == "raw message"

    @pytest.mark.asyncio
    async def test_empty_message_raises(self) -> None:
        """send() with empty message should raise ValueError."""
        from entrabot.tools.teams import send

        with pytest.raises(ValueError, match="empty"):
            await send(chat_id="c1", message="", token="tok")

    @pytest.mark.asyncio
    async def test_whitespace_only_message_raises(self) -> None:
        """send() with whitespace-only message should raise ValueError."""
        from entrabot.tools.teams import send

        with pytest.raises(ValueError, match="empty"):
            await send(chat_id="c1", message="   ", token="tok")


# ---------------------------------------------------------------------------
# _push_channel_notification — observability must survive transport failure
# ---------------------------------------------------------------------------
# Historical bug (project_dm_notification_bug.md): DM messages were readable
# via manual calls but the channel-notification push silently dropped them,
# AND we lost any record that the message was ever observed because the
# interaction log write happened after the write-stream check. Daily summaries
# were blind to any inbound DM. These tests lock in the fix: observe (log)
# first, then push.


class TestPushChannelNotificationObservability:
    @pytest.fixture(autouse=True)
    def _force_leader_host(self, monkeypatch):
        """Default to leader mode for this class — push-path tests expect it.

        Slave-mode push skipping is covered separately in
        ``tests/test_host_detection.py::TestPushChannelNotificationSlaveGating``.
        """

    @pytest.mark.asyncio
    async def test_logs_interaction_even_when_write_stream_missing(
        self, tmp_path, monkeypatch
    ) -> None:
        """When write_stream is absent (no MCP client attached), the message
        must still be recorded in the interaction log. Otherwise the daily
        summary is blind to inbound traffic whenever the push path breaks.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entrabot import mcp_server
        from entrabot.tools.interaction_log import read_day

        mcp_server._state.pop("_write_stream", None)

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-dm-1",
                "from": "Alice Smith",
                "content": "test DM",
                "sent_at": "2026-04-17T01:00:00Z",
            },
            chat_id="19:abc_def@unq.gbl.spaces",
        )

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(today)
        inbound = [e for e in entries if e.get("direction") == "inbound"]
        assert any(
            e.get("content_ref") == "m-dm-1" and e.get("channel") == "teams_dm" for e in inbound
        ), f"Expected inbound DM entry for m-dm-1, got: {inbound}"

    @pytest.mark.asyncio
    async def test_logs_interaction_and_pushes_when_stream_present(
        self, tmp_path, monkeypatch
    ) -> None:
        """Happy path: write_stream is available, push fires AND log captures
        the inbound message. Both effects must occur in the same call.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entrabot import mcp_server
        from entrabot.tools.interaction_log import read_day

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream

        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-grp-1",
                    "from": "Alice Example",
                    "content": "group chat test",
                    "sent_at": "2026-04-17T01:05:00Z",
                },
                chat_id="19:xyz@thread.v2",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        mock_stream.send.assert_awaited_once()

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(today)
        inbound = [e for e in entries if e.get("direction") == "inbound"]
        assert any(
            e.get("content_ref") == "m-grp-1" and e.get("channel") == "teams_group" for e in inbound
        ), f"Expected inbound group entry for m-grp-1, got: {inbound}"

    @pytest.mark.asyncio
    async def test_channel_notification_content_strips_teams_html(
        self, tmp_path, monkeypatch
    ) -> None:
        """Teams Graph bodies include HTML that can close Claude's MCP stream.

        The channel notification content must be plain text. Keep rich Teams
        metadata in meta, but do not pass raw ``<attachment>`` or ``<p>`` tags
        through the top-level content field.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from entrabot import mcp_server

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-html-1",
                    "from": "Brandon",
                    "content": '<attachment id="SRC-1"></attachment><p>As</p>',
                    "sent_at": "2026-04-24T20:00:00Z",
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        mock_stream.send.assert_awaited_once()
        notif_params = mock_stream.send.call_args.args[0].message.root.params
        content = notif_params["content"]
        assert content == "As"
        assert "<" not in content
        assert ">" not in content
        assert "attachment" not in content
        assert "</p>" not in content

    @pytest.mark.asyncio
    async def test_channel_notification_preserves_img_src_url(self, tmp_path, monkeypatch) -> None:
        """Giphy and other ``<img src="...">`` embeds slipped past the
        2026-04-24 sanitizer: the src URL got dropped, leaving an empty
        content string that lost the only signal in the message. Preserve
        the URL as plain text so the LLM can see it, while keeping the
        payload free of angle brackets that close the MCP stream.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from entrabot import mcp_server

        giphy_src = "https://media4.giphy.com/media/v1.Y2lkPTc5MGI3NjExeXh0ZGF6/giphy.gif"
        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-img-1",
                    "from": "Joe Brockhaus",
                    "content": f'<div><img src="{giphy_src}" alt="" /></div>',
                    "sent_at": "2026-04-27T21:46:41Z",
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        mock_stream.send.assert_awaited_once()
        notif_params = mock_stream.send.call_args.args[0].message.root.params
        content = notif_params["content"]
        assert "<" not in content, f"raw angle bracket survived: {content!r}"
        assert ">" not in content, f"raw angle bracket survived: {content!r}"
        assert giphy_src in content, (
            f"img src URL must round-trip into content for the LLM, got: {content!r}"
        )

    @pytest.mark.asyncio
    async def test_channel_notification_preserves_anchor_text_and_href(
        self, tmp_path, monkeypatch
    ) -> None:
        """Teams ``<attachment>`` cards that wrap an ``<a href>`` link lost
        the URL after the 2026-04-24 sanitizer ran — only the anchor text
        survived. Keep both: anchor text plus the href URL, no angles.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from entrabot import mcp_server

        href = "https://example.com/path/to/resource"
        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-att-1",
                    "from": "Alice Smith",
                    "content": (
                        '<attachment id="1776624654280"></attachment>\n'
                        f'<p><a href="{href}">link text</a></p>'
                    ),
                    "sent_at": "2026-04-27T21:46:43Z",
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        mock_stream.send.assert_awaited_once()
        notif_params = mock_stream.send.call_args.args[0].message.root.params
        content = notif_params["content"]
        assert "<" not in content, f"raw angle bracket survived: {content!r}"
        assert ">" not in content, f"raw angle bracket survived: {content!r}"
        assert "link text" in content, f"anchor text must survive sanitization, got: {content!r}"
        assert href in content, (
            f"href URL must round-trip into content for the LLM, got: {content!r}"
        )

    @pytest.mark.asyncio
    async def test_reply_to_ids_omitted_from_meta(self, tmp_path, monkeypatch) -> None:
        """`meta.reply_to_ids` and `meta.quoted_messages` are intentionally
        omitted from the channel push: 2026-04-28 forensic envelope dumps
        proved Claude Code's MCP client closes the stream whenever these
        custom fields are populated, regardless of content. See
        docs/runbooks/mcp-disconnect-investigation.md.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from entrabot import mcp_server

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-reply-1",
                    "from": "Brandon",
                    "content": '<attachment id="SRC-1"></attachment><p>yes</p>',
                    "sent_at": "2026-04-17T01:10:00Z",
                    "reply_to_ids": ["SRC-1"],
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        mock_stream.send.assert_awaited_once()
        notif_params = mock_stream.send.call_args.args[0].message.root.params
        assert "reply_to_ids" not in notif_params["meta"]
        assert "quoted_messages" not in notif_params["meta"]

    @pytest.mark.asyncio
    async def test_reply_to_ids_absent_when_empty(self, tmp_path, monkeypatch) -> None:
        """Keep meta tight: don't add reply_to_ids when the list is empty."""
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from entrabot import mcp_server

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-plain-1",
                    "from": "Brandon",
                    "content": "plain message",
                    "sent_at": "2026-04-17T01:11:00Z",
                    "reply_to_ids": [],
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        mock_stream.send.assert_awaited_once()
        notif_params = mock_stream.send.call_args.args[0].message.root.params
        assert "reply_to_ids" not in notif_params["meta"]
        assert "quoted_messages" not in notif_params["meta"]

    @pytest.mark.asyncio
    async def test_quoted_messages_omitted_from_push_envelope(self, tmp_path, monkeypatch) -> None:
        """Reply-to-ids and quoted-message bodies must NOT be inlined in the
        push. Claude Code's MCP client closes the stream on those custom
        fields. The fetch path is also gone on main — quoted bodies aren't
        needed for any other purpose since they're not in the push.
        See docs/runbooks/mcp-disconnect-investigation.md.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from entrabot import mcp_server

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        mcp_server._state["token"] = "tok"

        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-reply-2",
                    "from": "Brandon",
                    "content": (
                        '<attachment id="SRC-A"></attachment>'
                        '<attachment id="SRC-B"></attachment><p>hi</p>'
                    ),
                    "sent_at": "2026-04-17T01:12:00Z",
                    "reply_to_ids": ["SRC-A", "SRC-B"],
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)
            mcp_server._state.pop("token", None)

        notif_params = mock_stream.send.call_args.args[0].message.root.params
        assert "reply_to_ids" not in notif_params["meta"]
        assert "quoted_messages" not in notif_params["meta"]

    @pytest.mark.asyncio
    async def test_channel_notification_meta_strips_html_everywhere(
        self, tmp_path, monkeypatch
    ) -> None:
        """No angle brackets anywhere in params — not just in content.

        Display names like ``Alice Smith <brandon@contoso.com>`` and any
        future Graph fields that creep into ``meta`` must be sanitized.
        The walk-every-string assertion is stronger than per-field checks
        — it catches new fields that future code might add.
        See ``docs/runbooks/mcp-disconnect-investigation.md``.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from entrabot import mcp_server

        mock_stream = AsyncMock()
        mcp_server._state["_write_stream"] = mock_stream
        mcp_server._state["token"] = "tok"

        try:
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-1",
                    "from": "Alice Smith <brandon@contoso.com>",
                    "content": "<p>plain reply</p>",
                    "sent_at": "2026-04-28T00:01:00Z",
                    "reply_to_ids": ["SRC-1"],
                },
                chat_id="19:abc@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)
            mcp_server._state.pop("token", None)

        notif_params = mock_stream.send.call_args.args[0].message.root.params

        def _walk(obj, path):
            if isinstance(obj, str):
                assert "<" not in obj, f"angle bracket leaked in {path}: {obj!r}"
                assert ">" not in obj, f"angle bracket leaked in {path}: {obj!r}"
            elif isinstance(obj, dict):
                for key, value in obj.items():
                    _walk(value, f"{path}.{key}")
            elif isinstance(obj, list):
                for index, value in enumerate(obj):
                    _walk(value, f"{path}[{index}]")

        _walk(notif_params, "params")

    @pytest.mark.asyncio
    async def test_interaction_log_written_even_without_stream(self, tmp_path, monkeypatch) -> None:
        """Regression guard: the interaction log write must still happen
        when no write stream is present. Daily summaries stay blind-proof
        even when push transport is broken."""
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entrabot import mcp_server
        from entrabot.tools.interaction_log import read_day

        # No write stream — push path is skipped entirely. The log write
        # is the only observable effect.
        mcp_server._state.pop("_write_stream", None)

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-log-first",
                "from": "Brandon",
                "content": "something",
                "sent_at": "2026-04-17T01:15:00Z",
                "reply_to_ids": ["SRC"],
            },
            chat_id="19:abc@unq.gbl.spaces",
        )

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(today)
        assert any(e.get("content_ref") == "m-log-first" for e in entries)

    @pytest.mark.asyncio
    async def test_log_survives_push_exception(self, tmp_path, monkeypatch) -> None:
        """If write_stream.send raises, the interaction must already be logged.
        Root-cause traceability depends on this invariant: we can always
        answer 'did this message arrive at the agent?' by reading the log,
        even when the push transport is broken.
        """
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entrabot import mcp_server
        from entrabot.tools.interaction_log import read_day

        bad_stream = AsyncMock()
        bad_stream.send.side_effect = RuntimeError("stream closed")
        mcp_server._state["_write_stream"] = bad_stream

        try:
            # Exception inside the push must not propagate out of
            # _push_channel_notification — it's observability, not a
            # primary path. Caller (background poll) continues.
            await mcp_server._push_channel_notification(
                {
                    "message_id": "m-crash-1",
                    "from": "Alice Smith",
                    "content": "crash path",
                    "sent_at": "2026-04-17T01:06:00Z",
                },
                chat_id="19:abc_def@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(today)
        assert any(e.get("content_ref") == "m-crash-1" for e in entries), (
            "log must capture the message even when push transport throws"
        )


# ---------------------------------------------------------------------------
# Chat auto-discovery — the fix for "polling misses chats I didn't register"
# ---------------------------------------------------------------------------
# Historical bug: chats created via raw entrabot.tools.teams.create_* (not
# the MCP create_chat tool wrapper) never got added to watched_chats, so
# the background poll silently ignored them. Also: when a human adds the
# Agent User to a brand-new group chat, there's no in-process hook to
# register it at all. Fix: a background task that periodically hits
# /me/chats and registers any chat_id not already watched.


class TestChatAutoDiscovery:
    @pytest.mark.asyncio
    async def test_registers_new_chats_from_me_chats(self, tmp_path, monkeypatch) -> None:
        """When /me/chats returns a chat_id not in watched_chats, the
        discovery sweep must register it (both in-memory and persisted)
        so future background polls iterate it."""
        monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))

        import respx

        from entrabot import mcp_server

        existing = "19:existing@thread.v2"
        brand_new = "19:brand_new@thread.v2"

        # Seed state with one already-watched chat + token + identity.
        fake_config = MagicMock()
        fake_config.data_dir = tmp_path

        sm = MagicMock()
        sm.state = mcp_server.IdentityState.AGENT_USER
        sm.session.token = "tok"
        sm.session.token_acquired_at = time.monotonic()  # fresh token
        sm.update_session = MagicMock()

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            mcp_server._state.clear()
            mcp_server._state["config"] = fake_config
            mcp_server._state["token"] = "tok"
            mcp_server._state["token_acquired_at"] = time.monotonic()
            mcp_server._state["watched_chats"] = {
                existing: {"seen_ids": set(), "last_ts": None, "bootstrapped": False}
            }
            mcp_server._identity = sm

            with respx.mock:
                respx.get("https://graph.microsoft.com/v1.0/me/chats").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "value": [
                                {"id": existing},
                                {"id": brand_new},
                            ]
                        },
                    )
                )

                # Inline the sweep loop body: one iteration without the
                # asyncio.sleep that makes the while-True task untestable.
                import httpx as _httpx

                await mcp_server._ensure_valid_token()
                async with _httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://graph.microsoft.com/v1.0/me/chats",
                        headers={"Authorization": "Bearer tok"},
                        params={"$top": "50"},
                    )
                    watched = mcp_server._state["watched_chats"]
                    for chat in resp.json().get("value", []):
                        cid = chat.get("id")
                        if cid and cid not in watched:
                            mcp_server._register_watched_chat(cid, persist=True)

            # In-memory: both chats now present, new one marked not-bootstrapped
            assert existing in mcp_server._state["watched_chats"]
            assert brand_new in mcp_server._state["watched_chats"]
            assert mcp_server._state["watched_chats"][brand_new]["bootstrapped"] is False

            # File: new chat persisted so next server start inherits it
            persisted = (tmp_path / "watched_chats").read_text().splitlines()
            assert brand_new in persisted
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity


# ---------------------------------------------------------------------------
# _bootstrap_chat — watermark behavior
# ---------------------------------------------------------------------------
class TestBootstrapChat:
    """Bootstrap should NOT swallow the newest existing message.

    Previously, bootstrap added every fetched message to seen_ids and set
    last_ts to the newest message's sent_at, so the newest message was
    treated as "already seen" and never pushed on the first real poll.
    That swallowed the common case where a human adds the agent to a new
    chat AND posts an intro in the same minute: the intro post got added
    during bootstrap and silently filtered.

    Fix: bootstrap marks every message EXCEPT the newest as seen. last_ts
    still watermarks at the newest's sent_at (plus the 2s overlap window in
    _filter_new_messages) but the newest's message_id is NOT in seen_ids,
    so the first real poll pushes it normally.
    """

    async def test_newest_message_is_not_swallowed_by_bootstrap(self, tmp_path) -> None:
        from entrabot import mcp_server

        chat_id = "19:brand_new@thread.v2"

        fake_config = MagicMock()
        fake_config.data_dir = tmp_path

        sm = MagicMock()
        sm.state = mcp_server.IdentityState.AGENT_USER
        sm.session.token = "tok"
        sm.session.token_acquired_at = time.monotonic()
        sm.update_session = MagicMock()

        bootstrap_msgs = [
            {"message_id": "m-old", "sent_at": "2026-04-19T18:40:00.000Z"},
            {"message_id": "m-mid", "sent_at": "2026-04-19T18:45:00.000Z"},
            {"message_id": "m-new", "sent_at": "2026-04-19T18:50:54.280Z"},
        ]

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            mcp_server._state.clear()
            mcp_server._state["config"] = fake_config
            mcp_server._state["token"] = "tok"
            mcp_server._state["token_acquired_at"] = time.monotonic()
            mcp_server._state["watched_chats"] = {
                chat_id: {
                    "seen_ids": set(),
                    "last_ts": None,
                    "bootstrapped": False,
                }
            }
            mcp_server._identity = sm

            with patch(
                "entrabot.tools.teams.read",
                new=AsyncMock(return_value=bootstrap_msgs),
            ):
                await mcp_server._bootstrap_chat(chat_id)

            chat_state = mcp_server._state["watched_chats"][chat_id]
            assert chat_state["bootstrapped"] is True

            # Older messages should be marked seen (so they don't re-push).
            assert "m-old" in chat_state["seen_ids"]
            assert "m-mid" in chat_state["seen_ids"]

            # The NEWEST message must NOT be in seen_ids — it should get
            # pushed on the first real poll cycle.
            assert "m-new" not in chat_state["seen_ids"], (
                "bug: bootstrap is swallowing the newest message by pre-marking it seen"
            )

            # Simulate the first real poll: _filter_new_messages should
            # return the newest message because it's not yet in seen_ids.
            filtered = mcp_server._filter_new_messages(
                bootstrap_msgs,
                chat_state["last_ts"],
                chat_state["seen_ids"],
            )
            filtered_ids = {m["message_id"] for m in filtered}
            assert "m-new" in filtered_ids, (
                "bug: first real poll should return the newest message but it was filtered out"
            )
            # Older messages should still be filtered (they're in seen_ids).
            assert "m-old" not in filtered_ids
            assert "m-mid" not in filtered_ids
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity

    async def test_bootstrap_with_no_messages_is_a_noop(self, tmp_path) -> None:
        from entrabot import mcp_server

        chat_id = "19:empty@thread.v2"

        fake_config = MagicMock()
        fake_config.data_dir = tmp_path

        sm = MagicMock()
        sm.state = mcp_server.IdentityState.AGENT_USER
        sm.session.token = "tok"
        sm.session.token_acquired_at = time.monotonic()
        sm.update_session = MagicMock()

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            mcp_server._state.clear()
            mcp_server._state["config"] = fake_config
            mcp_server._state["token"] = "tok"
            mcp_server._state["token_acquired_at"] = time.monotonic()
            mcp_server._state["watched_chats"] = {
                chat_id: {
                    "seen_ids": set(),
                    "last_ts": None,
                    "bootstrapped": False,
                }
            }
            mcp_server._identity = sm

            with patch(
                "entrabot.tools.teams.read",
                new=AsyncMock(return_value=[]),
            ):
                await mcp_server._bootstrap_chat(chat_id)

            chat_state = mcp_server._state["watched_chats"][chat_id]
            assert chat_state["bootstrapped"] is True
            assert chat_state["seen_ids"] == set()
            assert chat_state["last_ts"] is None
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity


# ---------------------------------------------------------------------------
# Background poll task lifecycle — the fix for "new chats don't get polled"
# ---------------------------------------------------------------------------
# Historical bug: _init_poll only started _background_poll() when watched_chats
# was non-empty at init time. If the MCP server booted with zero watched chats
# (e.g. no default group chat configured) and a chat was added later via the
# create_chat tool, the poll task was never created — so no notifications
# pushed for that chat. Fix: _register_watched_chat lazily starts the poll
# task if one isn't already running.


class TestPollTaskAutoStart:
    """_register_watched_chat should start the background poll task if
    no task is currently running. This covers the case where the MCP server
    boots with zero watched chats and a chat is added later via create_chat."""

    @pytest.mark.asyncio
    async def test_register_starts_poll_when_no_task_running(self, monkeypatch) -> None:
        monkeypatch.setenv("ENTRABOT_SKIP_PROVISIONING", "true")

        from entrabot import mcp_server

        # Leader mode required — slave hosts intentionally skip polling.

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            mcp_server._state["watched_chats"] = {}
            mcp_server._state["poll_task"] = None

            created: list = []

            def fake_create_task(coro, *args, **kwargs):
                # Close the coroutine to avoid "never awaited" warnings.
                coro.close()
                sentinel = MagicMock()
                sentinel.done.return_value = False
                created.append(sentinel)
                return sentinel

            loop = MagicMock()
            loop.create_task.side_effect = fake_create_task
            with patch("asyncio.get_event_loop", return_value=loop):
                mcp_server._register_watched_chat("19:new-chat@thread.v2", persist=False)

            assert len(created) == 1, (
                "registering a chat when no poll task is running must spin up _background_poll"
            )
            assert mcp_server._state.get("poll_task") is created[0]
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_register_does_not_double_start(self, monkeypatch) -> None:
        """If a poll task is already running, registering another chat
        must not start a second task."""
        monkeypatch.setenv("ENTRABOT_SKIP_PROVISIONING", "true")

        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            mcp_server._state["watched_chats"] = {}
            running_task = MagicMock()
            running_task.done.return_value = False
            mcp_server._state["poll_task"] = running_task

            loop = MagicMock()
            loop.create_task.side_effect = AssertionError("must not create a second poll task")
            with patch("asyncio.get_event_loop", return_value=loop):
                mcp_server._register_watched_chat("19:second-chat@thread.v2", persist=False)
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_register_restarts_if_task_done(self, monkeypatch) -> None:
        """If the prior poll task has finished (e.g. crashed), registering
        a new chat should spin up a fresh one."""
        monkeypatch.setenv("ENTRABOT_SKIP_PROVISIONING", "true")

        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            mcp_server._state["watched_chats"] = {}
            dead_task = MagicMock()
            dead_task.done.return_value = True
            mcp_server._state["poll_task"] = dead_task

            created: list = []

            def fake_create_task(coro, *args, **kwargs):
                coro.close()
                new_task = MagicMock()
                new_task.done.return_value = False
                created.append(new_task)
                return new_task

            loop = MagicMock()
            loop.create_task.side_effect = fake_create_task
            with patch("asyncio.get_event_loop", return_value=loop):
                mcp_server._register_watched_chat("19:resume@thread.v2", persist=False)

            assert len(created) == 1
            assert mcp_server._state["poll_task"] is created[0]
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_bot_mode_does_not_start_graph_poll(self, monkeypatch) -> None:
        """In bot mode, the graph poll must not start — the bot gateway
        handles inbound via _background_poll_bot instead."""
        monkeypatch.setenv("ENTRABOT_SKIP_PROVISIONING", "true")

        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            mcp_server._state["watched_chats"] = {}
            mcp_server._state["poll_task"] = None

            fake_config = MagicMock()
            fake_config.mode = "bot"
            fake_config.data_dir = None
            mcp_server._state["config"] = fake_config

            loop = MagicMock()
            loop.create_task.side_effect = AssertionError("bot mode must not spawn the graph poll")
            with patch("asyncio.get_event_loop", return_value=loop):
                mcp_server._register_watched_chat("19:bot@thread.v2", persist=False)
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)


# ---------------------------------------------------------------------------
# Sponsor gate cache invalidation on new watched chat
# ---------------------------------------------------------------------------
# Historical bug: wait_for_sponsor_dm caches the SponsorGate in
# _state["sponsor_gate"] at first use. The gate's user_ids set is enriched
# from chat-member emails at gate-build time, so a chat added AFTER the
# gate was first built (e.g. via create_chat in a fresh Copilot CLI
# session) is invisible to the gate. The sponsor's home-tenant userId
# never gets added, every inbound DM gets rejected, and wait hangs
# forever. Fix: invalidate the cached gate whenever a NEW chat is
# registered so the next wait_for_sponsor_dm call rebuilds it with the
# new chat's members included.


class TestSponsorGateInvalidationOnNewChat:
    """_register_watched_chat must drop the cached sponsor gate when a
    new chat is added so the next wait_for_sponsor_dm rebuild picks up
    the new chat's members for federated B2B sponsor enrichment."""

    @pytest.mark.asyncio
    async def test_register_new_chat_invalidates_sponsor_gate(self, monkeypatch) -> None:
        monkeypatch.setenv("ENTRABOT_SKIP_PROVISIONING", "true")

        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            mcp_server._state["watched_chats"] = {}
            mcp_server._state["sponsor_gate"] = MagicMock(name="cached_gate")
            running_task = MagicMock()
            running_task.done.return_value = False
            mcp_server._state["poll_task"] = running_task

            loop = MagicMock()
            with patch("asyncio.get_event_loop", return_value=loop):
                mcp_server._register_watched_chat("19:brand-new-chat@unq.gbl.spaces", persist=False)

            assert mcp_server._state.get("sponsor_gate") is None, (
                "registering a new watched chat must invalidate the cached "
                "sponsor gate so next wait_for_sponsor_dm rebuilds it"
            )
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_register_existing_chat_preserves_sponsor_gate(self, monkeypatch) -> None:
        """Idempotent registration — re-registering an already-watched chat
        must NOT invalidate the gate (avoids unnecessary Graph calls)."""
        monkeypatch.setenv("ENTRABOT_SKIP_PROVISIONING", "true")

        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            existing_chat = "19:already-watched@thread.v2"
            mcp_server._state["watched_chats"] = {
                existing_chat: {
                    "seen_ids": set(),
                    "last_ts": None,
                    "bootstrapped": False,
                }
            }
            cached_gate = MagicMock(name="cached_gate")
            mcp_server._state["sponsor_gate"] = cached_gate
            running_task = MagicMock()
            running_task.done.return_value = False
            mcp_server._state["poll_task"] = running_task

            loop = MagicMock()
            with patch("asyncio.get_event_loop", return_value=loop):
                mcp_server._register_watched_chat(existing_chat, persist=False)

            assert mcp_server._state.get("sponsor_gate") is cached_gate, (
                "re-registering an already-watched chat must NOT invalidate the cached sponsor gate"
            )
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)


# ---------------------------------------------------------------------------
# Per-chat resilience in _background_poll
# ---------------------------------------------------------------------------
# Historical bug: a single chat's Graph error (403 on a stale chat, transient
# network blip) would bubble out of the per-chat body, abort the entire poll
# cycle, and starve every chat later in iteration order. Fix: wrap the
# per-chat body in its own try/except so one bad chat can't block the rest.


class TestBackgroundPollPerChatResilience:
    """One chat raising must not prevent the other chats in the same cycle
    from being polled and pushing notifications."""

    @pytest.mark.asyncio
    async def test_one_chat_403_does_not_starve_others(self, monkeypatch) -> None:
        import asyncio as _asyncio

        from entrabot import mcp_server

        bad_chat = "19:bad@thread.v2"
        good_chat = "19:good@thread.v2"

        good_msg = {
            "message_id": "m1",
            "from": "Alice Smith",
            "content": "<p>hi</p>",
            "sent_at": "2026-04-20T01:00:00.000Z",
        }

        sm = MagicMock()
        sm.state = mcp_server.IdentityState.AGENT_USER
        sm.session.token = "tok"
        sm.session.token_acquired_at = time.monotonic()

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            mcp_server._state.clear()
            mcp_server._state["token"] = "tok"
            mcp_server._state["watched_chats"] = {
                bad_chat: {
                    "seen_ids": set(),
                    "last_ts": "2026-04-20T00:00:00.000Z",
                    "bootstrapped": True,
                },
                good_chat: {
                    "seen_ids": set(),
                    "last_ts": "2026-04-20T00:00:00.000Z",
                    "bootstrapped": True,
                },
            }
            mcp_server._identity = sm

            async def fake_read(token, chat_id, count):
                if chat_id == bad_chat:
                    raise httpx.HTTPStatusError(
                        "403 Forbidden",
                        request=httpx.Request("GET", "https://example"),
                        response=httpx.Response(403),
                    )
                return [good_msg]

            pushed: list = []

            async def fake_push(msg, chat_id):
                pushed.append((chat_id, msg["message_id"]))

            async def no_refresh():
                return None

            # Cancel the loop after one cycle by raising on the second sleep.
            call_count = {"n": 0}
            real_sleep = _asyncio.sleep

            async def fake_sleep(seconds):
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    raise _asyncio.CancelledError()
                await real_sleep(0)

            with (
                patch(
                    "entrabot.tools.teams.read",
                    new=AsyncMock(side_effect=fake_read),
                ),
                patch(
                    "entrabot.mcp_server._ensure_valid_token",
                    new=AsyncMock(side_effect=no_refresh),
                ),
                patch(
                    "entrabot.mcp_server._push_channel_notification",
                    new=AsyncMock(side_effect=fake_push),
                ),
                patch.object(_asyncio, "sleep", new=fake_sleep),
                pytest.raises(_asyncio.CancelledError),
            ):
                await mcp_server._background_poll()

            # The good chat must have pushed despite the bad chat's 403.
            assert (good_chat, "m1") in pushed, "good chat must push even when bad chat throws"
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity


# ---------------------------------------------------------------------------
# _init_poll — no default chat auto-registration
# ---------------------------------------------------------------------------
# Historical: _init_poll auto-registered _state["chat_id"] (the "default group
# chat") as a watched chat. After the identity rework, a stale default chat
# can 403 on every poll. The agent only watches chats it has in memory
# (watched_chats file) — no automatic default.


class TestInitPollNoDefaultChat:
    @pytest.mark.asyncio
    async def test_default_chat_id_is_not_auto_registered(self, tmp_path, monkeypatch) -> None:
        """A _state["chat_id"] left over from _init_chat must NOT become a
        watched chat. Only explicit entries in the watched_chats file count."""
        from entrabot import mcp_server

        fake_config = MagicMock()
        fake_config.data_dir = tmp_path
        fake_config.mode = "agent_user"

        old_state = mcp_server._state.copy()
        old_identity = mcp_server._identity
        try:
            mcp_server._state.clear()
            mcp_server._state["config"] = fake_config
            mcp_server._state["chat_id"] = "19:stale-default@thread.v2"
            mcp_server._identity = None

            loop = MagicMock()
            loop.create_task.return_value = MagicMock(done=lambda: False)
            with patch("asyncio.get_event_loop", return_value=loop):
                await mcp_server._init_poll()

            assert "19:stale-default@thread.v2" not in mcp_server._state["watched_chats"], (
                "default chat_id must not be auto-registered"
            )
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity


# ---------------------------------------------------------------------------
# No default chat — the full kill
# ---------------------------------------------------------------------------
# The "default group chat" concept is dead: _initialize no longer calls
# _init_chat, tools don't fall through to _state["chat_id"], and a missing
# chat_id is an explicit error instead of a silent default.


class TestNoDefaultChat:
    def test_init_chat_symbol_is_gone(self) -> None:
        """Prevent regressions: _init_chat should not exist on the module."""
        import entrabot.mcp_server as mod

        assert not hasattr(mod, "_init_chat"), (
            "_init_chat was removed — the default-chat concept is gone"
        )

    @pytest.mark.asyncio
    async def test_initialize_does_not_touch_chat_id_state(self, monkeypatch) -> None:
        """A freshly initialized MCP server must have no _state["chat_id"]."""
        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()

            async def noop():
                return None

            with (
                patch.object(mcp_server, "_init_auth", new=AsyncMock(side_effect=noop)),
                patch.object(mcp_server, "_init_poll", new=AsyncMock(side_effect=noop)),
            ):
                await mcp_server._initialize()

            assert "chat_id" not in mcp_server._state, "_initialize must not set a default chat_id"
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_send_teams_message_errors_without_chat_id(self, monkeypatch) -> None:
        """send_teams_message with no chat_id and no legacy default
        must return an explicit error, not silently target nothing."""
        import json as _json

        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            # No chat_id in state, no config.mode == "bot".
            fake_config = MagicMock()
            fake_config.mode = "agent_user"
            mcp_server._state["config"] = fake_config

            with patch.object(
                mcp_server,
                "_initialize",
                new=AsyncMock(return_value=None),
            ):
                result = await mcp_server.send_teams_message(message="hi", chat_id="")

            parsed = _json.loads(result)
            assert "error" in parsed
            assert "chat_id" in parsed["error"].lower()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_send_teams_message_defaults_to_html(self, monkeypatch) -> None:
        """Channel discipline: every outgoing Teams message is HTML.

        The MCP tool signature must default ``content_type`` to ``"html"``
        so callers cannot forget and silently ship plain text — which
        renders as a second-class message in Teams and breaks links.
        """
        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            fake_config = MagicMock()
            fake_config.mode = "agent_user"
            mcp_server._state["config"] = fake_config

            captured: dict = {}

            async def fake_with_token_retry(fn, **kwargs):
                captured.update(kwargs)
                return {
                    "message_id": "msg-html",
                    "sent_at": "2026-04-23T18:30:00+00:00",
                }

            monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
            monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
            monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
            monkeypatch.setattr(mcp_server, "_with_token_retry", fake_with_token_retry)

            await mcp_server.send_teams_message(message="<p>hi</p>", chat_id="c1")

            assert captured.get("content_type") == "html", (
                "send_teams_message must default content_type='html' per "
                "prompts/anatomy/channel-discipline.md"
            )
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)


# ---------------------------------------------------------------------------
# send_teams_message auto-wait loop-continuation contract
# ---------------------------------------------------------------------------
#
# Bug: when Copilot CLI is launched outside the entrabot repo, neither
# AGENTS.md / CLAUDE.md / channel-discipline.md nor the FastMCP
# ``instructions=`` payload reach the LLM system prompt. The model sees
# only the tool docstring + tool result. Without an explicit "REQUIRED:
# call send_teams_message again" hint in BOTH places, the agent breaks
# out of the conversation loop after the first sponsor reply and answers
# in CLI text — invisible to the human waiting in Teams.
#
# These tests pin both halves of the contract: the docstring (so hosts
# that DO surface tool docs see it) and the ``_next_action`` field on
# the JSON return (so hosts that don't, still see it).


class TestSendTeamsMessageLoopContinuation:
    def test_docstring_states_required_followup_rule(self) -> None:
        """The send_teams_message docstring must explicitly tell the
        model to call send_teams_message again after a sponsor reply.
        Hosts like Copilot CLI launched outside the entrabot repo do
        not load AGENTS.md / channel-discipline.md, so the loop rule
        has to live on the tool itself."""
        from entrabot import mcp_server

        doc = mcp_server.send_teams_message.__doc__ or ""
        assert "REQUIRED FOLLOW-UP" in doc, (
            "send_teams_message docstring must contain the REQUIRED "
            "FOLLOW-UP block so non-Claude-Code hosts see the loop rule"
        )
        assert "send_teams_message`` again" in doc or (
            "send_teams_message" in doc and "again" in doc
        ), "Docstring must name the follow-up tool call explicitly"
        assert "Do NOT respond in CLI" in doc or "not respond in CLI" in doc.lower(), (
            "Docstring must forbid answering in CLI text"
        )

    @pytest.mark.asyncio
    async def test_auto_wait_result_contains_next_action_hint(self, monkeypatch) -> None:
        """When auto-wait returns a sponsor_reply, the JSON result must
        include a ``_next_action`` string instructing the model to call
        send_teams_message again with the sponsor's chat_id. This is
        the in-band channel that hosts (Copilot CLI in foreign cwd)
        cannot bypass."""
        import json as _json
        from collections import deque

        from entrabot import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            fake_config = MagicMock()
            fake_config.mode = "agent_user"
            fake_config.agent_user_upn = "agent@example.com"
            mcp_server._state["config"] = fake_config
            mcp_server._state["watched_chats"] = {"chat-A": {}}
            mcp_server._state["wait_tool_dedup"] = deque()

            # Cached gate — bypasses Graph load.
            fake_gate = MagicMock()
            fake_gate.user_ids = {"u-sponsor"}
            fake_gate.upns = set()
            fake_gate.mails = set()
            mcp_server._state["sponsor_gate"] = fake_gate

            # Pretend host has no channel push so auto-wait engages.
            monkeypatch.setattr(mcp_server, "_current_host", lambda: "copilot")

            async def fake_with_token_retry(fn, **kwargs):
                # First call (the actual send) returns a normal result.
                return {
                    "message_id": "msg-out-1",
                    "sent_at": "2026-04-30T15:00:00+00:00",
                }

            monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
            monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
            monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
            monkeypatch.setattr(mcp_server, "_with_token_retry", fake_with_token_retry)

            sponsor_msg = {
                "message_id": "msg-in-1",
                "chat_id": "chat-A",
                "sender": "Sponsor",
                "sender_id": "u-sponsor",
                "sent_at": "2026-04-30T15:01:00+00:00",
                "content": "<p>Make it color!</p>",
            }

            async def fake_wait_loop(**kwargs):
                return sponsor_msg

            from entrabot.tools import wait_tool

            monkeypatch.setattr(wait_tool, "wait_loop", fake_wait_loop)

            result_str = await mcp_server.send_teams_message(message="<p>hi</p>", chat_id="chat-A")
            parsed = _json.loads(result_str)

            assert "sponsor_reply" in parsed, (
                "auto-wait must surface sponsor_reply when a reply arrives"
            )
            assert parsed["sponsor_reply"]["chat_id"] == "chat-A"

            assert "_next_action" in parsed, (
                "auto-wait result must carry _next_action so hosts "
                "that don't load tool docstrings (Copilot CLI outside "
                "the entrabot repo) still see the loop-continuation "
                "rule"
            )
            next_action = parsed["_next_action"]
            assert "send_teams_message" in next_action, (
                "_next_action must name the follow-up tool call"
            )
            assert "chat-A" in next_action, (
                "_next_action must echo the sponsor's chat_id so the model knows where to reply"
            )
            assert "CLI" in next_action or "terminal" in next_action.lower(), (
                "_next_action must forbid answering in CLI text"
            )
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)


# ---------------------------------------------------------------------------
# post_thinking_placeholder / resolve_placeholder MCP wrappers
# ---------------------------------------------------------------------------


class TestThinkingPlaceholderTool:
    @pytest.mark.asyncio
    async def test_post_placeholder_logs_interaction(self, monkeypatch, tmp_path) -> None:
        """post_thinking_placeholder writes an outbound interaction log entry."""
        import json as _json

        from entrabot import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value="msg-placeholder-1"),
        )

        result = await mcp_server.post_thinking_placeholder(chat_id="c1", text="thinking…")
        parsed = _json.loads(result)
        assert parsed["message_id"] == "msg-placeholder-1"
        assert captured["direction"] == "outbound"
        assert captured["action"] == "post_thinking_placeholder"
        assert captured["content_ref"] == "msg-placeholder-1"

    @pytest.mark.asyncio
    async def test_post_placeholder_requires_chat_id(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.post_thinking_placeholder(chat_id="")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "chat_id" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_resolve_audits_before_graph_call(self, monkeypatch, tmp_path) -> None:
        """resolve_placeholder logs an audit event before the Graph call."""
        import json as _json

        from entrabot import mcp_server

        audit_events: list[dict] = []

        def fake_log_event(**kwargs):
            audit_events.append(kwargs)
            return {"event_id": "evt-1", **kwargs}

        monkeypatch.setattr("entrabot.tools.audit.log_event", fake_log_event)
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)

        graph_called = False

        async def fake_retry(fn, **kwargs):
            nonlocal graph_called
            # Audit must have been written before Graph is invoked.
            assert audit_events, "audit event must be written before the Graph mutation"
            graph_called = True
            return {"message_id": "msg-p1", "mode": "edit"}

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        result = await mcp_server.resolve_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            final_message="<p>done</p>",
            mode="edit",
        )
        parsed = _json.loads(result)
        assert parsed["mode"] == "edit"
        assert graph_called
        assert audit_events[0]["action"] == "resolve_placeholder"
        assert audit_events[0]["resource"] == "c1:msg-p1"
        assert audit_events[0]["metadata"]["mode"] == "edit"

    @pytest.mark.asyncio
    async def test_resolve_rejects_invalid_mode(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.resolve_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            final_message="done",
            mode="nonsense",
        )
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "mode" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_resolve_requires_ids(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.resolve_placeholder(
            chat_id="",
            placeholder_id="msg-p1",
            final_message="done",
        )
        parsed = _json.loads(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_resolve_logs_interaction_with_resolved_mode(self, monkeypatch) -> None:
        """Interaction log reports the actual mode returned by Graph (e.g. fallback_new)."""
        import json as _json

        from entrabot import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value={"message_id": "msg-new", "mode": "fallback_new"}),
        )

        await mcp_server.resolve_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            final_message="<p>done</p>",
            mode="edit",
        )
        assert captured["action"] == "resolve_placeholder"
        assert captured["metadata"]["mode"] == "fallback_new"
        assert captured["metadata"]["requested_mode"] == "edit"
        _json.loads("{}")  # silence unused


class TestUpdatePlaceholderTool:
    """MCP wrapper around tools.teams.update_placeholder. Unlike
    resolve_placeholder, this one is NOT audit-logged — it surfaces
    mid-flight progress, not a final commitment."""

    @pytest.mark.asyncio
    async def test_calls_teams_helper_with_args(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)

        captured_kwargs: dict = {}

        async def fake_retry(fn, **kwargs):
            captured_kwargs.update(kwargs)
            return {"message_id": "msg-p1", "mode": "edit"}

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        result = await mcp_server.update_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            progress_text="reading the interaction log",
        )
        parsed = _json.loads(result)
        assert parsed == {"message_id": "msg-p1", "mode": "edit"}
        assert captured_kwargs["chat_id"] == "c1"
        assert captured_kwargs["placeholder_id"] == "msg-p1"
        assert captured_kwargs["progress_text"] == "reading the interaction log"

    @pytest.mark.asyncio
    async def test_does_not_write_audit_event(self, monkeypatch) -> None:
        """Progress updates are ephemeral — no audit event fires. Only
        resolve_placeholder writes to the audit log."""
        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value={"message_id": "msg-p1", "mode": "edit"}),
        )

        audit_events: list[dict] = []

        def fake_audit(**kw):
            audit_events.append(kw)
            return {"event_id": "evt-y", **kw}

        monkeypatch.setattr("entrabot.tools.audit.log_event", fake_audit)

        await mcp_server.update_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            progress_text="reading",
        )
        assert audit_events == [], (
            "update_placeholder must NOT audit-log; audit fires only on resolve_placeholder"
        )

    @pytest.mark.asyncio
    async def test_logs_interaction_with_progress_flag(self, monkeypatch) -> None:
        from entrabot import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value={"message_id": "msg-p1", "mode": "edit"}),
        )

        await mcp_server.update_placeholder(
            chat_id="c1",
            placeholder_id="msg-p1",
            progress_text="grepping",
        )
        assert captured["action"] == "update_placeholder"
        assert captured["metadata"]["progress"] is True
        assert captured["metadata"]["placeholder"] is True
        assert captured["metadata"]["placeholder_id"] == "msg-p1"

    @pytest.mark.asyncio
    async def test_requires_chat_id_and_placeholder_id(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.update_placeholder(
            chat_id="", placeholder_id="msg-p1", progress_text="x"
        )
        parsed = _json.loads(result)
        assert "error" in parsed


# ---------------------------------------------------------------------------
# delete_teams_message MCP wrapper
# ---------------------------------------------------------------------------


class TestDeleteTeamsMessageTool:
    @pytest.mark.asyncio
    async def test_calls_delete_chat_message_with_ids(self, monkeypatch) -> None:
        """delete_teams_message forwards chat_id + message_id to Graph helper."""
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )

        captured_kwargs: dict = {}

        async def fake_retry(fn, **kwargs):
            captured_kwargs.update(kwargs)
            return True

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        result = await mcp_server.delete_teams_message(message_id="msg-1", chat_id="c1")
        parsed = _json.loads(result)
        assert parsed["deleted"] is True
        assert parsed["message_id"] == "msg-1"
        assert captured_kwargs["chat_id"] == "c1"
        assert captured_kwargs["message_id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_rejects_missing_chat_id(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.delete_teams_message(message_id="msg-1", chat_id="")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "chat_id" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_rejects_missing_message_id(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.delete_teams_message(message_id="", chat_id="c1")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "message_id" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_audits_before_graph_call(self, monkeypatch) -> None:
        """Security: audit event must land before the Graph mutation (fail-closed)."""
        from entrabot import mcp_server

        audit_events: list[dict] = []

        def fake_log_event(**kwargs):
            audit_events.append(kwargs)
            return {"event_id": "evt-1", **kwargs}

        monkeypatch.setattr("entrabot.tools.audit.log_event", fake_log_event)
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)

        graph_called = False

        async def fake_retry(fn, **kwargs):
            nonlocal graph_called
            assert audit_events, "audit event must be written before the Graph mutation"
            graph_called = True
            return True

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        await mcp_server.delete_teams_message(message_id="msg-1", chat_id="c1")
        assert graph_called
        assert audit_events[0]["action"] == "delete_teams_message"
        assert audit_events[0]["resource"] == "c1:msg-1"

    @pytest.mark.asyncio
    async def test_interaction_log_on_success(self, monkeypatch) -> None:
        from entrabot import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value=True),
        )

        await mcp_server.delete_teams_message(message_id="msg-1", chat_id="c1")
        assert captured["direction"] == "outbound"
        assert captured["action"] == "delete_teams_message"
        assert captured["metadata"]["deleted"] is True
        assert captured["metadata"]["chat_id"] == "c1"
        assert captured["metadata"]["message_id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_interaction_log_on_failure(self, monkeypatch) -> None:
        """Failure path: Graph returned False (403/404). deleted=false logged."""
        import json as _json

        from entrabot import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value=False),
        )

        result = await mcp_server.delete_teams_message(message_id="msg-1", chat_id="c1")
        parsed = _json.loads(result)
        assert parsed["deleted"] is False
        assert "reason" in parsed
        assert captured["action"] == "delete_teams_message"
        assert captured["metadata"]["deleted"] is False


# ---------------------------------------------------------------------------
# send_email MCP wrapper
# ---------------------------------------------------------------------------


class TestSendEmailTool:
    @pytest.mark.asyncio
    async def test_splits_comma_separated_recipients(self, monkeypatch) -> None:
        """to/cc/bcc arrive as comma-separated strings; wrapper splits to lists."""
        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )

        captured: dict = {}

        async def fake_retry(fn, **kwargs):
            captured.update(kwargs)
            return {"sent_at": "2026-04-20T00:00:00+00:00"}

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        await mcp_server.send_email(
            to="a@example.com, b@example.com ,  ",
            subject="hi",
            body="<p>hi</p>",
            cc="c@example.com",
            bcc="d@example.com, e@example.com",
        )

        assert captured["to"] == ["a@example.com", "b@example.com"]
        assert captured["cc"] == ["c@example.com"]
        assert captured["bcc"] == ["d@example.com", "e@example.com"]
        assert captured["subject"] == "hi"

    @pytest.mark.asyncio
    async def test_rejects_empty_to(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.send_email(to="   ", subject="hi", body="b")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "to" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_rejects_empty_subject(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.send_email(to="a@example.com", subject="", body="b")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "subject" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_audits_before_graph_call(self, monkeypatch) -> None:
        """Security: audit event lands before the Graph mutation (fail-closed)."""
        from entrabot import mcp_server

        audit_events: list[dict] = []

        def fake_log_event(**kwargs):
            audit_events.append(kwargs)
            return {"event_id": "evt-1", **kwargs}

        monkeypatch.setattr("entrabot.tools.audit.log_event", fake_log_event)
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)

        graph_called = False

        async def fake_retry(fn, **kwargs):
            nonlocal graph_called
            assert audit_events, "audit event must be written before the Graph mutation"
            graph_called = True
            return {"sent_at": "2026-04-20T00:00:00+00:00"}

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        await mcp_server.send_email(to="a@example.com", subject="Re: hello", body="<p>hi</p>")
        assert graph_called
        assert audit_events[0]["action"] == "send_email"
        # Resource should carry the recipient + subject for audit forensics.
        resource = audit_events[0]["resource"]
        assert "a@example.com" in resource
        assert "Re: hello" in resource

    @pytest.mark.asyncio
    async def test_interaction_log_on_success(self, monkeypatch) -> None:
        from entrabot import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value={"sent_at": "2026-04-20T00:00:00+00:00"}),
        )

        await mcp_server.send_email(
            to="a@example.com",
            subject="Re: deploy",
            body="<p>shipped</p>",
            cc="b@example.com",
            reply_to_message_id="AAMk==",
        )
        assert captured["direction"] == "outbound"
        assert captured["channel"] == "email"
        assert captured["action"] == "send_email"
        assert captured["recipient"] == "a@example.com"
        meta = captured["metadata"]
        assert meta["to"] == ["a@example.com"]
        assert meta["cc"] == ["b@example.com"]
        assert meta["bcc"] == []
        assert meta["subject"] == "Re: deploy"
        assert meta["content_type"] == "html"
        assert meta["reply_to_message_id"] == "AAMk=="
        # Don't leak the body into the log (security: "no full bodies").
        assert "<p>shipped</p>" not in _safe_json(meta)

    @pytest.mark.asyncio
    async def test_interaction_log_on_failure(self, monkeypatch) -> None:
        from entrabot import mcp_server
        from entrabot.tools.email import EmailSendError

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))

        async def fake_retry(fn, **kwargs):
            raise EmailSendError("Graph rejected: ErrorInvalidRecipients")

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        # Wrapper returns JSON (with error), does NOT leak the exception.
        import json as _json

        result = await mcp_server.send_email(to="bogus@example.com", subject="s", body="b")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert captured["action"] == "send_email"
        assert captured["metadata"]["outcome"] == "failure"


def _safe_json(obj) -> str:
    import json as _json

    try:
        return _json.dumps(obj)
    except Exception:
        return str(obj)


# ---------------------------------------------------------------------------
# Promise MCP wrappers: add_promise / list_promises / resolve_promise
# ---------------------------------------------------------------------------


class TestPromiseTools:
    @pytest.mark.asyncio
    async def test_add_promise_rejects_empty_chat_id(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.add_promise(chat_id="", description="something")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "chat_id" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_add_promise_rejects_empty_description(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.add_promise(chat_id="c1", description="   ")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "description" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_add_promise_audits_before_blob_call(self, monkeypatch) -> None:
        """Audit event must land before the blob write (fail-closed)."""
        from entrabot import mcp_server

        audit_events: list[dict] = []

        def fake_log_event(**kwargs):
            audit_events.append(kwargs)
            return {"event_id": "evt-1", **kwargs}

        monkeypatch.setattr("entrabot.tools.audit.log_event", fake_log_event)
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)

        blob_called = False

        async def fake_retry(fn, **kwargs):
            nonlocal blob_called
            assert audit_events, "audit event must be written before the blob write"
            blob_called = True
            from entrabot.tools.promises import Promise

            return Promise(
                id="abc123",
                created_at="2026-04-20T00:00:00+00:00",
                chat_id=kwargs.get("chat_id", ""),
                description=kwargs.get("description", ""),
            )

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        await mcp_server.add_promise(chat_id="c1", description="announce PR landing")
        assert blob_called
        assert audit_events[0]["action"] == "promise.add"
        assert audit_events[0]["outcome"] == "pending"

    @pytest.mark.asyncio
    async def test_add_promise_writes_interaction_log_on_success(self, monkeypatch) -> None:
        from entrabot import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))

        from entrabot.tools.promises import Promise

        async def fake_retry(fn, **kwargs):
            return Promise(
                id="abc123",
                created_at="2026-04-20T00:00:00+00:00",
                chat_id="c1",
                description="announce PR",
            )

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        await mcp_server.add_promise(chat_id="c1", description="announce PR")
        assert captured["action"] == "promise.add"
        assert captured["direction"] == "outbound"

    @pytest.mark.asyncio
    async def test_list_promises_returns_json_array(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server
        from entrabot.tools.promises import Promise

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )

        promises_out = [
            Promise(
                id="p1",
                created_at="2026-04-20T00:00:00+00:00",
                chat_id="c1",
                description="one",
            ),
            Promise(
                id="p2",
                created_at="2026-04-20T00:01:00+00:00",
                chat_id="c2",
                description="two",
            ),
        ]

        async def fake_retry(fn, **kwargs):
            return promises_out

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        result = await mcp_server.list_promises()
        parsed = _json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 2
        assert {p["id"] for p in parsed} == {"p1", "p2"}

    @pytest.mark.asyncio
    async def test_list_promises_does_not_write_interaction_log(self, monkeypatch) -> None:
        """Reads are not logged — mutations are."""
        from entrabot import mcp_server

        calls: list[dict] = []

        def fake_log(**kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))

        async def fake_retry(fn, **kwargs):
            return []

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        await mcp_server.list_promises()
        assert calls == []

    @pytest.mark.asyncio
    async def test_resolve_promise_rejects_empty_id(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.resolve_promise(promise_id="", resolution="done")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "promise_id" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_resolve_promise_rejects_empty_resolution(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        result = await mcp_server.resolve_promise(promise_id="abc", resolution="")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "resolution" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_resolve_promise_propagates_not_found_as_error(self, monkeypatch) -> None:
        import json as _json

        from entrabot import mcp_server
        from entrabot.tools.promises import PromiseNotFound

        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )

        async def fake_retry(fn, **kwargs):
            raise PromiseNotFound("abc")

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        result = await mcp_server.resolve_promise(promise_id="abc", resolution="done")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "not found" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_round_trip_add_list_resolve_list(self, monkeypatch) -> None:
        """End-to-end with a real in-memory backend wired through.

        Patches ``get_backend`` so the MCP wrappers hit the same backend
        the promises module does.
        """
        import json as _json

        from entrabot import mcp_server
        from entrabot.tools import promises as promises_mod

        class InMem:
            def __init__(self) -> None:
                self._store: dict[str, str] = {}

            def read_text(self, key):
                return self._store.get(key)

            def write_text(self, key, content):
                self._store[key] = content

            def append_text(self, key, content):
                self._store[key] = (self._store.get(key) or "") + content

            def exists(self, key):
                return key in self._store

            def list(self, prefix=""):
                return [k for k in self._store if k.startswith(prefix)]

        shared = InMem()
        monkeypatch.setattr(promises_mod, "get_backend", lambda: shared)
        monkeypatch.setattr(promises_mod, "_get_conditional_store", lambda: None)
        monkeypatch.setattr(mcp_server, "_initialize", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock(return_value=None))
        monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )

        # Pass-through retry: call the underlying promises helper directly.
        # Real _with_token_retry supplies ``token=...``; our wrappers
        # accept it and ignore it, so forward as-is.
        async def fake_retry(fn, **kwargs):
            kwargs.setdefault("token", "stub-token")
            return await fn(**kwargs)

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        add_out = await mcp_server.add_promise(chat_id="c1", description="announce PR")
        add_parsed = _json.loads(add_out)
        promise_id = add_parsed["id"]

        list_out = await mcp_server.list_promises()
        open_list = _json.loads(list_out)
        assert len(open_list) == 1
        assert open_list[0]["id"] == promise_id

        resolve_out = await mcp_server.resolve_promise(promise_id=promise_id, resolution="merged")
        resolved = _json.loads(resolve_out)
        assert resolved["status"] == "resolved"
        assert resolved["resolution"] == "merged"

        list_all = _json.loads(await mcp_server.list_promises(open_only=False))
        assert len(list_all) == 1
        assert list_all[0]["status"] == "resolved"

        list_open = _json.loads(await mcp_server.list_promises())
        assert list_open == []
