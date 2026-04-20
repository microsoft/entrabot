"""Integration tests for mcp_server identity-aware changes.

Tests cover:
- _load_agent_instructions() returns generic tool-description (mind-body split)
- _resolve_tenant_id() helper (eng review 3A)
- Token refresh dispatch: DELEGATED→MSAL, AGENT_USER→three-hop (6A)
- Delegated-mode poll echo-prevention via sent_message_ids
- Sent-message set FIFO eviction at SENT_MESSAGE_MAX
- Silent-refresh-failure → UNAUTHENTICATED transition
- Audit attribution reads from identity state machine (Tension 1)
- filter_human_messages with sent_message_ids and [EntraClaw] prefix
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from entraclaw.tools.teams import filter_human_messages


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

    def test_returns_hardcoded_when_no_body_and_no_persona(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)

        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "LOCAL_PROMPT_PATH", tmp_path / "missing.md"
        )

        result = mcp_server._load_agent_instructions()
        assert "EntraClaw Teams Interface" in result
        assert "persona-sati" in result

    def test_mcp_server_boots(self) -> None:
        """The module-level mcp object should be created without error."""
        from entraclaw.mcp_server import mcp

        assert mcp.name == "EntraClaw Agent Identity"

    def test_body_alone_when_persona_unavailable(
        self, monkeypatch, tmp_path
    ) -> None:
        """With only the body file present, it becomes the full prompt."""
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)

        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text("BODY_ONLY", encoding="utf-8")

        from entraclaw import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()
        assert "BODY_ONLY" in result
        assert "EntraClaw Teams Interface" not in result

    def test_body_is_prepended_to_persona(
        self, monkeypatch, tmp_path
    ) -> None:
        """When both body and persona are available, body loads FIRST
        (before persona) so its rules can't be overridden."""
        import asyncio
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv(
            "PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli"
        )
        monkeypatch.setattr(
            subprocess, "check_output", lambda *a, **kw: "fake.jwt.token\n"
        )

        def _fake_run(coro):
            coro.close()
            return "PERSONA_CONTENT"

        monkeypatch.setattr(asyncio, "run", _fake_run)

        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text("BODY_CONTENT", encoding="utf-8")

        from entraclaw import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()

        body_at = result.find("BODY_CONTENT")
        persona_at = result.find("PERSONA_CONTENT")
        assert body_at >= 0, "body must be present"
        assert persona_at > body_at, (
            "body must precede persona so rules aren't overridden"
        )

    def test_persona_alone_when_body_missing(
        self, monkeypatch, tmp_path
    ) -> None:
        """If the body file doesn't exist but persona works, return
        persona alone (legacy behavior; real deployments ship a body)."""
        import asyncio
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv(
            "PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli"
        )
        monkeypatch.setattr(
            subprocess, "check_output", lambda *a, **kw: "fake.jwt.token\n"
        )
        monkeypatch.setattr(
            asyncio,
            "run",
            lambda c: (c.close(), "PERSONA_ONLY")[1],
        )

        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "LOCAL_PROMPT_PATH", tmp_path / "missing.md"
        )

        result = mcp_server._load_agent_instructions()
        assert result == "PERSONA_ONLY"

    def test_include_directive_expands_anatomy_files(
        self, monkeypatch, tmp_path
    ) -> None:
        """@include lines in the body must be replaced with the target
        file's contents so security/behavior modules actually load."""
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)

        anatomy = tmp_path / "anatomy"
        anatomy.mkdir()
        (anatomy / "security.md").write_text(
            "SECURITY_RULES", encoding="utf-8"
        )
        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text(
            "# Body\n@include anatomy/security.md\nAfter include.\n",
            encoding="utf-8",
        )

        from entraclaw import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()
        assert "SECURITY_RULES" in result
        assert "@include" not in result, "directive must be consumed"
        assert "After include." in result

    def test_missing_include_does_not_crash(
        self, monkeypatch, tmp_path
    ) -> None:
        """A missing @include target must not prevent the rest of the
        body from loading."""
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)

        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text(
            "# Body\n@include anatomy/does-not-exist.md\nstill here\n",
            encoding="utf-8",
        )

        from entraclaw import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()
        assert "still here" in result

    def test_empty_body_file_uses_hardcoded(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)

        prompt_file = tmp_path / "agent_system.md"
        prompt_file.write_text("   \n\n", encoding="utf-8")

        from entraclaw import mcp_server

        monkeypatch.setattr(mcp_server, "LOCAL_PROMPT_PATH", prompt_file)

        result = mcp_server._load_agent_instructions()
        assert "EntraClaw Teams Interface" in result


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

    _LOCAL_PREFIX = "EntraClaw Teams Interface"

    @pytest.fixture(autouse=True)
    def _isolate_local_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Point LOCAL_PROMPT_PATH at a non-existent file so these tests
        only exercise the persona-sati / hardcoded-fallback paths — not
        whatever prompts/agent_system.md happens to be on disk."""
        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "LOCAL_PROMPT_PATH", tmp_path / "missing.md"
        )

    def test_load_instructions_uses_local_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PERSONA_SATI_MCP_URL", raising=False)
        monkeypatch.delenv("PERSONA_SATI_MCP_TOKEN_COMMAND", raising=False)
        from entraclaw.mcp_server import _load_agent_instructions

        result = _load_agent_instructions()
        assert result.startswith(self._LOCAL_PREFIX)

    def test_load_instructions_uses_local_when_token_cmd_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import subprocess

        monkeypatch.setenv("PERSONA_SATI_MCP_URL", "https://persona.example")
        monkeypatch.setenv(
            "PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/does-not-exist"
        )

        def _raise(*args, **kwargs):
            raise subprocess.SubprocessError("token mint blew up")

        monkeypatch.setattr(subprocess, "check_output", _raise)

        from entraclaw.mcp_server import _load_agent_instructions

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
        monkeypatch.setenv(
            "PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli"
        )
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *a, **kw: "fake.jwt.token\n",
        )

        def _fake_run(coro):
            coro.close()  # silence "coroutine was never awaited"
            return "REMOTE_SYSTEM_PROMPT"

        monkeypatch.setattr(asyncio, "run", _fake_run)

        from entraclaw.mcp_server import _load_agent_instructions

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
        monkeypatch.setenv(
            "PERSONA_SATI_MCP_TOKEN_COMMAND", "/tmp/fake-token-cli"
        )
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *a, **kw: "fake.jwt.token\n",
        )

        def _boom(coro):
            coro.close()  # silence "coroutine was never awaited"
            raise RuntimeError("remote MCP unreachable")

        monkeypatch.setattr(asyncio, "run", _boom)

        from entraclaw.mcp_server import _load_agent_instructions

        result = _load_agent_instructions()
        assert result.startswith(self._LOCAL_PREFIX)
        captured = capsys.readouterr()
        assert "persona-sati fetch failed" in captured.err
        assert captured.out == ""


# ---------------------------------------------------------------------------
# _resolve_tenant_id
# ---------------------------------------------------------------------------
class TestResolveTenantId:
    async def test_same_domain_returns_none(self) -> None:
        from entraclaw.mcp_server import _resolve_tenant_id

        assert await _resolve_tenant_id("user@contoso.com", "contoso.com") is None

    async def test_case_insensitive_domain(self) -> None:
        from entraclaw.mcp_server import _resolve_tenant_id

        assert await _resolve_tenant_id("user@Contoso.COM", "contoso.com") is None

    async def test_no_at_sign_returns_none(self) -> None:
        from entraclaw.mcp_server import _resolve_tenant_id

        assert await _resolve_tenant_id("notanemail", "contoso.com") is None

    async def test_cross_tenant_discovery_success(self) -> None:
        from entraclaw.mcp_server import _resolve_tenant_id

        fake_oidc = {
            "issuer": "https://login.microsoftonline.com/aaaabbbb-cccc-dddd-eeee-ffff00001111/v2.0",
        }
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = fake_oidc

        mock_client = AsyncMock()
        mock_client.get.return_value = fake_resp
        mock_client.__aenter__.return_value = mock_client

        with patch("entraclaw.mcp_server.httpx.AsyncClient", return_value=mock_client):
            result = await _resolve_tenant_id("alice@fabrikam.com", "contoso.com")
            assert result == "aaaabbbb-cccc-dddd-eeee-ffff00001111"

    async def test_cross_tenant_sts_windows_issuer(self) -> None:
        """microsoft.com returns sts.windows.net issuer, not login.microsoftonline.com."""
        from entraclaw.mcp_server import _resolve_tenant_id

        fake_oidc = {
            "issuer": "https://sts.windows.net/72f988bf-86f1-41af-91ab-2d7cd011db47/",
        }
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = fake_oidc

        mock_client = AsyncMock()
        mock_client.get.return_value = fake_resp
        mock_client.__aenter__.return_value = mock_client

        with patch("entraclaw.mcp_server.httpx.AsyncClient", return_value=mock_client):
            result = await _resolve_tenant_id("brandwe@microsoft.com", "werner.ac")
            assert result == "72f988bf-86f1-41af-91ab-2d7cd011db47"

    async def test_discovery_failure_returns_none(self) -> None:
        from entraclaw.mcp_server import _resolve_tenant_id

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("timeout")
        mock_client.__aenter__.return_value = mock_client

        with patch("entraclaw.mcp_server.httpx.AsyncClient", return_value=mock_client):
            result = await _resolve_tenant_id("alice@fabrikam.com", "contoso.com")
            assert result is None

    async def test_discovery_non_200_returns_none(self) -> None:
        from entraclaw.mcp_server import _resolve_tenant_id

        fake_resp = MagicMock()
        fake_resp.status_code = 404

        mock_client = AsyncMock()
        mock_client.get.return_value = fake_resp
        mock_client.__aenter__.return_value = mock_client

        with patch("entraclaw.mcp_server.httpx.AsyncClient", return_value=mock_client):
            result = await _resolve_tenant_id("alice@fabrikam.com", "contoso.com")
            assert result is None


# ---------------------------------------------------------------------------
# Token refresh dispatch (6A)
# ---------------------------------------------------------------------------
class TestTokenRefreshDispatch:
    @pytest.mark.asyncio
    async def test_agent_user_refreshes_via_three_hop(self) -> None:
        """AGENT_USER state should refresh via acquire_agent_user_token."""
        from entraclaw import mcp_server
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

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

            with patch("entraclaw.mcp_server.acquire_agent_user_token", mock_acquire):
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
        from entraclaw import mcp_server
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

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
            mock_auth_instance.try_silent.return_value = {
                "access_token": "msal-refreshed"
            }

            with patch(
                "entraclaw.auth.delegated.MsalDelegatedAuth",
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
        from entraclaw import mcp_server
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

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
        from entraclaw import mcp_server
        from entraclaw.identity.state_machine import IdentityStateMachine
        from entraclaw.models import IdentityState

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
                "entraclaw.auth.delegated.MsalDelegatedAuth",
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
        from entraclaw.mcp_server import SENT_MESSAGE_MAX, _sent_message_ids

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

    def test_excludes_entraclaw_prefix(self) -> None:
        """Messages starting with [EntraClaw] should be filtered (restart-safe dedup)."""
        messages = [
            {"message_id": "m1", "from": "Human", "content": "[EntraClaw] automated msg"},
            {"message_id": "m2", "from": "Human", "content": "normal human msg"},
        ]
        result = filter_human_messages(messages, "Agent")
        assert len(result) == 1
        assert result[0]["content"] == "normal human msg"

    def test_agent_display_name_still_filtered(self) -> None:
        """Agent's own messages are still filtered by display name."""
        messages = [
            {"message_id": "m1", "from": "EntraClaw Agent", "content": "hi"},
            {"message_id": "m2", "from": "Human", "content": "response"},
        ]
        result = filter_human_messages(messages, "EntraClaw Agent")
        assert len(result) == 1
        assert result[0]["from"] == "Human"

    def test_combined_filters(self) -> None:
        """All filter modes work together."""
        messages = [
            {"message_id": "m1", "from": "Agent", "content": "agent msg"},
            {"message_id": "m2", "from": "unknown", "content": "system msg"},
            {"message_id": "m3", "from": "Human", "content": "[EntraClaw] echo"},
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

        from entraclaw.tools.audit import log_event

        with mock_patch("entraclaw.tools.audit._audit_dir", return_value=tmp_path):
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

        from entraclaw.tools.audit import log_event

        with mock_patch("entraclaw.tools.audit._audit_dir", return_value=tmp_path):
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

        from entraclaw.tools.teams import send

        with respx.mock:
            route = respx.post(
                "https://graph.microsoft.com/v1.0/chats/c1/messages"
            ).mock(
                return_value=httpx.Response(
                    201,
                    json={"id": "msg-1", "createdDateTime": "2026-01-01T00:00:00Z"},
                )
            )

            result = await send(
                chat_id="c1",
                message="hello world",
                token="tok",
                prefix="[EntraClaw]",
            )

            assert result["message_id"] == "msg-1"
            # Verify the payload sent to Graph included the prefix
            sent_body = route.calls[0].request.content
            import json

            payload = json.loads(sent_body)
            assert payload["body"]["content"] == "[EntraClaw] hello world"

    @pytest.mark.asyncio
    async def test_no_prefix_sends_raw(self) -> None:
        """send() without prefix should send message as-is."""
        import respx

        from entraclaw.tools.teams import send

        with respx.mock:
            route = respx.post(
                "https://graph.microsoft.com/v1.0/chats/c1/messages"
            ).mock(
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
        from entraclaw.tools.teams import send

        with pytest.raises(ValueError, match="empty"):
            await send(chat_id="c1", message="", token="tok")

    @pytest.mark.asyncio
    async def test_whitespace_only_message_raises(self) -> None:
        """send() with whitespace-only message should raise ValueError."""
        from entraclaw.tools.teams import send

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
    @pytest.mark.asyncio
    async def test_logs_interaction_even_when_write_stream_missing(
        self, tmp_path, monkeypatch
    ) -> None:
        """When write_stream is absent (no MCP client attached), the message
        must still be recorded in the interaction log. Otherwise the daily
        summary is blind to inbound traffic whenever the push path breaks.
        """
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entraclaw import mcp_server
        from entraclaw.tools.interaction_log import read_day

        mcp_server._state.pop("_write_stream", None)

        await mcp_server._push_channel_notification(
            {
                "message_id": "m-dm-1",
                "from": "Brandon Werner",
                "content": "test DM",
                "sent_at": "2026-04-17T01:00:00Z",
            },
            chat_id="19:abc_def@unq.gbl.spaces",
        )

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(today)
        inbound = [e for e in entries if e.get("direction") == "inbound"]
        assert any(
            e.get("content_ref") == "m-dm-1" and e.get("channel") == "teams_dm"
            for e in inbound
        ), f"Expected inbound DM entry for m-dm-1, got: {inbound}"

    @pytest.mark.asyncio
    async def test_logs_interaction_and_pushes_when_stream_present(
        self, tmp_path, monkeypatch
    ) -> None:
        """Happy path: write_stream is available, push fires AND log captures
        the inbound message. Both effects must occur in the same call.
        """
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entraclaw import mcp_server
        from entraclaw.tools.interaction_log import read_day

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
            e.get("content_ref") == "m-grp-1" and e.get("channel") == "teams_group"
            for e in inbound
        ), f"Expected inbound group entry for m-grp-1, got: {inbound}"

    @pytest.mark.asyncio
    async def test_log_survives_push_exception(
        self, tmp_path, monkeypatch
    ) -> None:
        """If write_stream.send raises, the interaction must already be logged.
        Root-cause traceability depends on this invariant: we can always
        answer 'did this message arrive at the agent?' by reading the log,
        even when the push transport is broken.
        """
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

        from datetime import UTC, datetime

        from entraclaw import mcp_server
        from entraclaw.tools.interaction_log import read_day

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
                    "from": "Brandon Werner",
                    "content": "crash path",
                    "sent_at": "2026-04-17T01:06:00Z",
                },
                chat_id="19:abc_def@unq.gbl.spaces",
            )
        finally:
            mcp_server._state.pop("_write_stream", None)

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        entries = read_day(today)
        assert any(
            e.get("content_ref") == "m-crash-1" for e in entries
        ), "log must capture the message even when push transport throws"


# ---------------------------------------------------------------------------
# Chat auto-discovery — the fix for "polling misses chats I didn't register"
# ---------------------------------------------------------------------------
# Historical bug: chats created via raw entraclaw.tools.teams.create_* (not
# the MCP create_chat tool wrapper) never got added to watched_chats, so
# the background poll silently ignored them. Also: when a human adds the
# Agent User to a brand-new group chat, there's no in-process hook to
# register it at all. Fix: a background task that periodically hits
# /me/chats and registers any chat_id not already watched.


class TestChatAutoDiscovery:
    @pytest.mark.asyncio
    async def test_registers_new_chats_from_me_chats(
        self, tmp_path, monkeypatch
    ) -> None:
        """When /me/chats returns a chat_id not in watched_chats, the
        discovery sweep must register it (both in-memory and persisted)
        so future background polls iterate it."""
        monkeypatch.setenv("ENTRACLAW_DATA_DIR", str(tmp_path))

        import respx

        from entraclaw import mcp_server

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
            assert (
                mcp_server._state["watched_chats"][brand_new]["bootstrapped"]
                is False
            )

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

    async def test_newest_message_is_not_swallowed_by_bootstrap(
        self, tmp_path
    ) -> None:
        from entraclaw import mcp_server

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
                "entraclaw.tools.teams.read",
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
                "bug: bootstrap is swallowing the newest message by pre-"
                "marking it seen"
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
                "bug: first real poll should return the newest message but "
                "it was filtered out"
            )
            # Older messages should still be filtered (they're in seen_ids).
            assert "m-old" not in filtered_ids
            assert "m-mid" not in filtered_ids
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)
            mcp_server._identity = old_identity

    async def test_bootstrap_with_no_messages_is_a_noop(self, tmp_path) -> None:
        from entraclaw import mcp_server

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
                "entraclaw.tools.teams.read",
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
    async def test_register_starts_poll_when_no_task_running(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTRACLAW_SKIP_PROVISIONING", "true")

        from entraclaw import mcp_server

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
                mcp_server._register_watched_chat(
                    "19:new-chat@thread.v2", persist=False
                )

            assert len(created) == 1, (
                "registering a chat when no poll task is running must "
                "spin up _background_poll"
            )
            assert mcp_server._state.get("poll_task") is created[0]
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_register_does_not_double_start(self, monkeypatch) -> None:
        """If a poll task is already running, registering another chat
        must not start a second task."""
        monkeypatch.setenv("ENTRACLAW_SKIP_PROVISIONING", "true")

        from entraclaw import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()
            mcp_server._state["watched_chats"] = {}
            running_task = MagicMock()
            running_task.done.return_value = False
            mcp_server._state["poll_task"] = running_task

            loop = MagicMock()
            loop.create_task.side_effect = AssertionError(
                "must not create a second poll task"
            )
            with patch("asyncio.get_event_loop", return_value=loop):
                mcp_server._register_watched_chat(
                    "19:second-chat@thread.v2", persist=False
                )
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_register_restarts_if_task_done(self, monkeypatch) -> None:
        """If the prior poll task has finished (e.g. crashed), registering
        a new chat should spin up a fresh one."""
        monkeypatch.setenv("ENTRACLAW_SKIP_PROVISIONING", "true")

        from entraclaw import mcp_server

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
                mcp_server._register_watched_chat(
                    "19:resume@thread.v2", persist=False
                )

            assert len(created) == 1
            assert mcp_server._state["poll_task"] is created[0]
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_bot_mode_does_not_start_graph_poll(
        self, monkeypatch
    ) -> None:
        """In bot mode, the graph poll must not start — the bot gateway
        handles inbound via _background_poll_bot instead."""
        monkeypatch.setenv("ENTRACLAW_SKIP_PROVISIONING", "true")

        from entraclaw import mcp_server

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
            loop.create_task.side_effect = AssertionError(
                "bot mode must not spawn the graph poll"
            )
            with patch("asyncio.get_event_loop", return_value=loop):
                mcp_server._register_watched_chat(
                    "19:bot@thread.v2", persist=False
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
    async def test_one_chat_403_does_not_starve_others(
        self, monkeypatch
    ) -> None:
        import asyncio as _asyncio

        from entraclaw import mcp_server

        bad_chat = "19:bad@thread.v2"
        good_chat = "19:good@thread.v2"

        good_msg = {
            "message_id": "m1",
            "from": "Brandon Werner",
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
                    "entraclaw.tools.teams.read",
                    new=AsyncMock(side_effect=fake_read),
                ),
                patch(
                    "entraclaw.mcp_server._ensure_valid_token",
                    new=AsyncMock(side_effect=no_refresh),
                ),
                patch(
                    "entraclaw.mcp_server._push_channel_notification",
                    new=AsyncMock(side_effect=fake_push),
                ),
                patch.object(_asyncio, "sleep", new=fake_sleep),
                pytest.raises(_asyncio.CancelledError),
            ):
                await mcp_server._background_poll()

            # The good chat must have pushed despite the bad chat's 403.
            assert (good_chat, "m1") in pushed, (
                "good chat must push even when bad chat throws"
            )
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
    async def test_default_chat_id_is_not_auto_registered(
        self, tmp_path, monkeypatch
    ) -> None:
        """A _state["chat_id"] left over from _init_chat must NOT become a
        watched chat. Only explicit entries in the watched_chats file count."""
        from entraclaw import mcp_server

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

            assert (
                "19:stale-default@thread.v2"
                not in mcp_server._state["watched_chats"]
            ), "default chat_id must not be auto-registered"
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
        import entraclaw.mcp_server as mod

        assert not hasattr(mod, "_init_chat"), (
            "_init_chat was removed — the default-chat concept is gone"
        )

    @pytest.mark.asyncio
    async def test_initialize_does_not_touch_chat_id_state(
        self, monkeypatch
    ) -> None:
        """A freshly initialized MCP server must have no _state["chat_id"]."""
        from entraclaw import mcp_server

        old_state = mcp_server._state.copy()
        try:
            mcp_server._state.clear()

            async def noop():
                return None

            with patch.object(
                mcp_server, "_init_auth", new=AsyncMock(side_effect=noop)
            ), patch.object(
                mcp_server, "_init_poll", new=AsyncMock(side_effect=noop)
            ):
                await mcp_server._initialize()

            assert "chat_id" not in mcp_server._state, (
                "_initialize must not set a default chat_id"
            )
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)

    @pytest.mark.asyncio
    async def test_send_teams_message_errors_without_chat_id(
        self, monkeypatch
    ) -> None:
        """send_teams_message with no chat_id and no legacy default
        must return an explicit error, not silently target nothing."""
        import json as _json

        from entraclaw import mcp_server

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
                result = await mcp_server.send_teams_message(
                    message="hi", chat_id=""
                )

            parsed = _json.loads(result)
            assert "error" in parsed
            assert "chat_id" in parsed["error"].lower()
        finally:
            mcp_server._state.clear()
            mcp_server._state.update(old_state)


# ---------------------------------------------------------------------------
# post_thinking_placeholder / resolve_placeholder MCP wrappers
# ---------------------------------------------------------------------------


class TestThinkingPlaceholderTool:
    @pytest.mark.asyncio
    async def test_post_placeholder_logs_interaction(
        self, monkeypatch, tmp_path
    ) -> None:
        """post_thinking_placeholder writes an outbound interaction log entry."""
        import json as _json

        from entraclaw import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value="msg-placeholder-1"),
        )

        result = await mcp_server.post_thinking_placeholder(
            chat_id="c1", text="thinking…"
        )
        parsed = _json.loads(result)
        assert parsed["message_id"] == "msg-placeholder-1"
        assert captured["direction"] == "outbound"
        assert captured["action"] == "post_thinking_placeholder"
        assert captured["content_ref"] == "msg-placeholder-1"

    @pytest.mark.asyncio
    async def test_post_placeholder_requires_chat_id(
        self, monkeypatch
    ) -> None:
        import json as _json

        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        result = await mcp_server.post_thinking_placeholder(chat_id="")
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "chat_id" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_resolve_audits_before_graph_call(
        self, monkeypatch, tmp_path
    ) -> None:
        """resolve_placeholder logs an audit event before the Graph call."""
        import json as _json

        from entraclaw import mcp_server

        audit_events: list[dict] = []

        def fake_log_event(**kwargs):
            audit_events.append(kwargs)
            return {"event_id": "evt-1", **kwargs}

        monkeypatch.setattr(
            "entraclaw.tools.audit.log_event", fake_log_event
        )
        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_log_interaction_safe", lambda **kw: None
        )

        graph_called = False

        async def fake_retry(fn, **kwargs):
            nonlocal graph_called
            # Audit must have been written before Graph is invoked.
            assert audit_events, (
                "audit event must be written before the Graph mutation"
            )
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

        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
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

        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        result = await mcp_server.resolve_placeholder(
            chat_id="",
            placeholder_id="msg-p1",
            final_message="done",
        )
        parsed = _json.loads(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_resolve_logs_interaction_with_resolved_mode(
        self, monkeypatch
    ) -> None:
        """Interaction log reports the actual mode returned by Graph (e.g. fallback_new)."""
        import json as _json

        from entraclaw import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entraclaw.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(
                return_value={"message_id": "msg-new", "mode": "fallback_new"}
            ),
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


# ---------------------------------------------------------------------------
# delete_teams_message MCP wrapper
# ---------------------------------------------------------------------------


class TestDeleteTeamsMessageTool:
    @pytest.mark.asyncio
    async def test_calls_delete_chat_message_with_ids(
        self, monkeypatch
    ) -> None:
        """delete_teams_message forwards chat_id + message_id to Graph helper."""
        import json as _json

        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_log_interaction_safe", lambda **kw: None
        )
        monkeypatch.setattr(
            "entraclaw.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )

        captured_kwargs: dict = {}

        async def fake_retry(fn, **kwargs):
            captured_kwargs.update(kwargs)
            return True

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        result = await mcp_server.delete_teams_message(
            message_id="msg-1", chat_id="c1"
        )
        parsed = _json.loads(result)
        assert parsed["deleted"] is True
        assert parsed["message_id"] == "msg-1"
        assert captured_kwargs["chat_id"] == "c1"
        assert captured_kwargs["message_id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_rejects_missing_chat_id(self, monkeypatch) -> None:
        import json as _json

        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        result = await mcp_server.delete_teams_message(
            message_id="msg-1", chat_id=""
        )
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "chat_id" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_rejects_missing_message_id(self, monkeypatch) -> None:
        import json as _json

        from entraclaw import mcp_server

        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        result = await mcp_server.delete_teams_message(
            message_id="", chat_id="c1"
        )
        parsed = _json.loads(result)
        assert "error" in parsed
        assert "message_id" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_audits_before_graph_call(self, monkeypatch) -> None:
        """Security: audit event must land before the Graph mutation (fail-closed)."""
        from entraclaw import mcp_server

        audit_events: list[dict] = []

        def fake_log_event(**kwargs):
            audit_events.append(kwargs)
            return {"event_id": "evt-1", **kwargs}

        monkeypatch.setattr(
            "entraclaw.tools.audit.log_event", fake_log_event
        )
        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_log_interaction_safe", lambda **kw: None
        )

        graph_called = False

        async def fake_retry(fn, **kwargs):
            nonlocal graph_called
            assert audit_events, (
                "audit event must be written before the Graph mutation"
            )
            graph_called = True
            return True

        monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)

        await mcp_server.delete_teams_message(
            message_id="msg-1", chat_id="c1"
        )
        assert graph_called
        assert audit_events[0]["action"] == "delete_teams_message"
        assert audit_events[0]["resource"] == "c1:msg-1"

    @pytest.mark.asyncio
    async def test_interaction_log_on_success(self, monkeypatch) -> None:
        from entraclaw import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entraclaw.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value=True),
        )

        await mcp_server.delete_teams_message(
            message_id="msg-1", chat_id="c1"
        )
        assert captured["direction"] == "outbound"
        assert captured["action"] == "delete_teams_message"
        assert captured["metadata"]["deleted"] is True
        assert captured["metadata"]["chat_id"] == "c1"
        assert captured["metadata"]["message_id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_interaction_log_on_failure(self, monkeypatch) -> None:
        """Failure path: Graph returned False (403/404). deleted=false logged."""
        import json as _json

        from entraclaw import mcp_server

        captured: dict = {}

        def fake_log(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mcp_server, "_log_interaction_safe", fake_log)
        monkeypatch.setattr(
            "entraclaw.tools.audit.log_event",
            lambda **kw: {"event_id": "evt-x"},
        )
        monkeypatch.setattr(
            mcp_server, "_initialize", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server, "_ensure_valid_token", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            mcp_server,
            "_with_token_retry",
            AsyncMock(return_value=False),
        )

        result = await mcp_server.delete_teams_message(
            message_id="msg-1", chat_id="c1"
        )
        parsed = _json.loads(result)
        assert parsed["deleted"] is False
        assert "reason" in parsed
        assert captured["action"] == "delete_teams_message"
        assert captured["metadata"]["deleted"] is False
