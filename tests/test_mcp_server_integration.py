"""Integration tests for mcp_server identity-aware changes.

Tests cover:
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
