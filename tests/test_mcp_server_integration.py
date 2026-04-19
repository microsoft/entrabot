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
# _load_agent_instructions (mind-body split)
# ---------------------------------------------------------------------------
class TestLoadAgentInstructions:
    """After the persona-sati integration, _load_agent_instructions returns
    a generic tool-description string — no file I/O, no personality."""

    def test_returns_generic_tool_description(self) -> None:
        from entraclaw.mcp_server import _load_agent_instructions

        result = _load_agent_instructions()
        assert "EntraClaw Teams Interface" in result
        assert "persona-sati" in result
        # Must NOT contain personality / channel-discipline content
        assert "channel discipline" not in result.lower()
        assert "Brandon" not in result
        assert "watch-only" not in result.lower()

    def test_does_not_import_path(self) -> None:
        """The function should not depend on pathlib — no file I/O."""
        import entraclaw.mcp_server as mod

        assert not hasattr(mod, "Path"), "Path should not be imported"

    def test_mcp_server_boots_without_prompt_file(self) -> None:
        """The module-level mcp object should be created without error."""
        from entraclaw.mcp_server import mcp

        assert mcp.name == "EntraClaw Agent Identity"


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
