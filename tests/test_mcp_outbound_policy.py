"""Outbound controls in the MCP tools: who may be reached, and audit that fails closed.

Every tool that reaches someone new checks each address against the server-side recipient
policy before Graph is called. Every tool that sends or creates writes its audit event
first and does nothing when that write fails.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from entrabot import mcp_server
from entrabot.config import EntraBotConfig

# Before any fixture replaces it.
_REAL_SPONSOR_ADDRESSES = mcp_server._sponsor_addresses

HOME_TENANT = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict:
    """An Agent User in contoso.com, one cross-tenant sponsor, Graph replaced."""
    config = EntraBotConfig(
        tenant_id=HOME_TENANT,
        agent_id="agent-app",
        agent_user_id="agent-user",
        agent_user_upn="entrabot-agent@contoso.com",
        human_user_mails=["sponsor@fabrikam.com"],
    )
    graph_calls: list[dict] = []
    audit: list[dict] = []

    async def fake_retry(fn, **kwargs):
        graph_calls.append({"fn": getattr(fn, "__name__", str(fn)), **kwargs})
        return {"chat_id": "19:new", "message_id": "m-1", "sent_at": "now"}

    def fake_log_event(**kwargs):
        audit.append(kwargs)
        return {"event_id": "e", **kwargs}

    async def tenant_of(email: str, our_domain: str) -> str | None:
        domain = email.split("@")[1].lower()
        return {"contoso.onmicrosoft.com": HOME_TENANT, "fabrikam.com": OTHER_TENANT}.get(domain)

    monkeypatch.setitem(mcp_server._state, "config", config)
    monkeypatch.setattr(mcp_server, "get_config", lambda: config)
    monkeypatch.setattr(mcp_server, "_identity", None)
    monkeypatch.setattr(mcp_server, "_initialize", AsyncMock())
    monkeypatch.setattr(mcp_server, "_ensure_valid_token", AsyncMock())
    monkeypatch.setattr(mcp_server, "_with_token_retry", fake_retry)
    monkeypatch.setattr(mcp_server, "_log_interaction_safe", lambda **kw: None)
    monkeypatch.setattr(mcp_server, "_persist_dirty_watched_chats", AsyncMock())
    monkeypatch.setattr(mcp_server, "_register_watched_chat", lambda *a, **k: None)
    monkeypatch.setattr(mcp_server, "_resolve_tenant_id", tenant_of)
    monkeypatch.setattr(
        mcp_server,
        "_sponsor_addresses",
        AsyncMock(return_value=frozenset({"sponsor@fabrikam.com"})),
    )
    monkeypatch.setattr(mcp_server, "_directory_sponsors", AsyncMock(return_value=None))
    monkeypatch.setattr("entrabot.tools.audit.log_event", fake_log_event)
    monkeypatch.delenv("ENTRABOT_OUTBOUND_ALLOW", raising=False)
    monkeypatch.delitem(mcp_server._state, "requesting_chat", raising=False)
    monkeypatch.delitem(mcp_server._state, "requesting_chat_members", raising=False)
    return {"graph": graph_calls, "audit": audit, "config": config}


def _sent(world: dict) -> list[str]:
    return [call["fn"] for call in world["graph"]]


class TestSendEmail:
    async def _send(self, **kwargs: str) -> dict:
        defaults = {"to": "pat@contoso.com", "subject": "hi", "body": "<p>hi</p>"}
        return json.loads(await mcp_server.send_email(**{**defaults, **kwargs}))

    async def test_home_tenant_is_allowed(self, world: dict) -> None:
        result = await self._send()
        assert "error" not in result
        assert world["graph"]

    async def test_a_domain_that_resolves_home_is_allowed(self, world: dict) -> None:
        assert "error" not in await self._send(to="pat@contoso.onmicrosoft.com")

    async def test_a_cross_tenant_sponsor_is_allowed(self, world: dict) -> None:
        assert "error" not in await self._send(to="Sponsor@Fabrikam.com")

    async def test_an_external_stranger_is_refused_before_graph(self, world: dict) -> None:
        result = await self._send(to="someone@gmail.com")
        assert result["error"] == "recipient not allowed: nothing was sent"
        assert result["refused"][0]["address"] == "someone@gmail.com"
        assert world["graph"] == []
        (event,) = world["audit"]
        assert event["outcome"] == "denied" and event["action"] == "send_email"

    async def test_one_refused_bcc_stops_the_whole_message(self, world: dict) -> None:
        result = await self._send(bcc="leak@gmail.com")
        assert "error" in result and world["graph"] == []

    async def test_the_allowlist_lets_an_external_address_through(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRABOT_OUTBOUND_ALLOW", "partner@gmail.com, @tailspin.example")
        assert "error" not in await self._send(to="partner@gmail.com, ops@tailspin.example")

    async def test_a_member_of_the_requesting_chat_is_allowed(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        monkeypatch.setitem(mcp_server._state, "requesting_chat", ("19:group", time.monotonic()))

        async def members(fn, **kwargs):
            if kwargs.get("chat_id") == "19:group" and "to" not in kwargs:
                return [{"user_id": "u", "name": "Guest", "email": "guest@gmail.com"}]
            world["graph"].append(kwargs)
            return {"sent_at": "now"}

        monkeypatch.setattr(mcp_server, "_with_token_retry", members)
        assert "error" not in await self._send(to="guest@gmail.com")

    async def test_unreadable_sponsors_count_as_none(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _sponsor_addresses itself turns a failed lookup into "no sponsors".
        monkeypatch.setattr(
            mcp_server, "_load_sponsor_gate", AsyncMock(side_effect=RuntimeError("graph down"))
        )
        monkeypatch.delitem(mcp_server._state, "_push_sponsor_gate", raising=False)
        monkeypatch.setattr(mcp_server, "_sponsor_addresses", _REAL_SPONSOR_ADDRESSES)
        result = await self._send(to="sponsor@fabrikam.com")
        assert "error" in result and world["graph"] == []


class TestOtherToolsThatReachSomeoneNew:
    async def test_create_chat_with_a_stranger_is_refused(self, world: dict) -> None:
        result = json.loads(await mcp_server.create_chat("someone@gmail.com"))
        assert result["error"] == "recipient not allowed: nothing was sent"
        assert world["graph"] == []

    async def test_create_chat_with_a_cross_tenant_sponsor_works(self, world: dict) -> None:
        result = json.loads(await mcp_server.create_chat("sponsor@fabrikam.com"))
        assert result.get("chat_id") == "19:new"
        actions = [(event["action"], event["outcome"]) for event in world["audit"]]
        assert actions == [("create_chat", "pending"), ("create_chat", "success")]

    async def test_add_teams_member_refuses_a_stranger_before_graph(self, world: dict) -> None:
        result = json.loads(
            await mcp_server.add_teams_member(
                chat_id="19:chat", email="someone@gmail.com", requester_email="sponsor@fabrikam.com"
            )
        )
        assert result["error"] == "recipient not allowed: nothing was sent"
        assert world["graph"] == []

    async def test_share_file_refuses_a_stranger_before_graph(self, world: dict) -> None:
        result = json.loads(
            await mcp_server.share_file(
                drive_id="d",
                item_id="i",
                name="plan.docx",
                recipient_email="someone@gmail.com",
                requester_email="sponsor@fabrikam.com",
                chat_id="19:chat",
            )
        )
        assert result["error"] == "recipient not allowed: nothing was sent"
        assert world["graph"] == []

    async def test_the_daily_summary_is_not_sent_to_an_external_non_sponsor(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world["config"].human_user_mails[:] = ["someone@gmail.com"]
        monkeypatch.setattr("entrabot.tools.interaction_log.read_day", lambda day: [])
        monkeypatch.setattr(
            "entrabot.tools.daily_summary.archive_summary", lambda **kwargs: "/tmp/summary.html"
        )
        result = await mcp_server._run_daily_summary_internal(day="2026-11-14")
        assert result["sent_to"] == [] and result["refused"]
        assert world["graph"] == []
        assert ("send_daily_summary", "denied") in [
            (event["action"], event["outcome"]) for event in world["audit"]
        ]


class TestDailySummaryRecipient:
    """The summary goes to the Agent Identity's sponsors as the directory lists them; .env
    is used only when the directory gives no sponsor address."""

    @pytest.fixture(autouse=True)
    def _no_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("entrabot.tools.interaction_log.read_day", lambda day: [])
        monkeypatch.setattr(
            "entrabot.tools.daily_summary.archive_summary", lambda **kwargs: "/tmp/summary.html"
        )

    @staticmethod
    def _sponsor(user_id: str, mail: str | None = None):
        from entrabot.identity.sponsors import AgentIdentitySponsor

        return AgentIdentitySponsor(user_id=user_id, user_principal_name=None, mail=mail)

    async def test_the_directory_sponsors_not_env(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world["config"].human_user_mails[:] = ["old-sponsor@contoso.com"]
        monkeypatch.setattr(
            mcp_server,
            "_directory_sponsors",
            AsyncMock(return_value=[self._sponsor("u-1", "pat@contoso.com")]),
        )
        result = await mcp_server._run_daily_summary_internal(day="2026-11-14")
        assert result["sent_to"] == ["pat@contoso.com"]
        assert result["recipient_source"] == "directory"
        assert [call["to"] for call in world["graph"]] == [["pat@contoso.com"]]

    async def test_every_sponsor_with_an_address(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sponsors = [
            self._sponsor("u-1", "pat@contoso.com"),
            self._sponsor("u-2", "lee@contoso.com"),
        ]
        monkeypatch.setattr(mcp_server, "_directory_sponsors", AsyncMock(return_value=sponsors))
        result = await mcp_server._run_daily_summary_internal(day="2026-11-14")
        assert result["sent_to"] == ["pat@contoso.com", "lee@contoso.com"]

    async def test_env_when_no_sponsor_record_can_be_read(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world["config"].human_user_mails[:] = ["pat@contoso.com"]
        monkeypatch.setattr(
            mcp_server, "_directory_sponsors", AsyncMock(return_value=[self._sponsor("u-1")])
        )
        result = await mcp_server._run_daily_summary_internal(day="2026-11-14")
        assert result["sent_to"] == ["pat@contoso.com"]
        assert result["recipient_source"] == "env"

    async def test_env_when_the_directory_cannot_be_read(self, world: dict) -> None:
        world["config"].human_user_mails[:] = ["pat@contoso.com"]
        result = await mcp_server._run_daily_summary_internal(day="2026-11-14")
        assert result["sent_to"] == ["pat@contoso.com"]
        assert result["recipient_source"] == "env"

    async def test_nothing_is_sent_with_no_address_at_all(self, world: dict) -> None:
        world["config"].human_user_mails[:] = []
        result = await mcp_server._run_daily_summary_internal(day="2026-11-14")
        assert result["sent_to"] == [] and world["graph"] == []


class TestAuditFailsClosed:
    @pytest.fixture
    def broken_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(**_kwargs: object) -> dict:
            raise OSError("disk full")

        monkeypatch.setattr("entrabot.tools.audit.log_event", fail)

    async def test_send_teams_message_does_not_send(self, world: dict, broken_audit) -> None:
        monkeypatch_free = mcp_server._check_placeholder_discipline
        try:
            mcp_server._check_placeholder_discipline = lambda message, chat: None
            result = json.loads(
                await mcp_server.send_teams_message(message="hello", chat_id="19:chat")
            )
        finally:
            mcp_server._check_placeholder_discipline = monkeypatch_free
        assert "audit event could not be recorded" in result["error"]
        assert world["graph"] == []

    async def test_send_card_does_not_send(self, world: dict, broken_audit) -> None:
        result = json.loads(
            await mcp_server.send_card(
                card_type="status", chat_id="19:chat", title="t", status="complete", detail="d"
            )
        )
        assert "error" in result and world["graph"] == []

    async def test_create_chat_does_not_create(self, world: dict, broken_audit) -> None:
        result = json.loads(await mcp_server.create_chat("pat@contoso.com"))
        assert "audit event could not be recorded" in result["error"]
        assert world["graph"] == []

    async def test_a_thinking_placeholder_is_not_posted(self, world: dict, broken_audit) -> None:
        result = json.loads(await mcp_server.post_thinking_placeholder(chat_id="19:chat"))
        assert "error" in result and world["graph"] == []

    async def test_send_email_does_not_send(self, world: dict, broken_audit) -> None:
        with pytest.raises(OSError):
            await mcp_server.send_email(to="pat@contoso.com", subject="s", body="b")
        assert world["graph"] == []


class TestModelAuthoredEvents:
    def test_audit_log_events_are_marked_as_the_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict] = []
        monkeypatch.setattr(
            "entrabot.tools.audit.log_event", lambda **kw: captured.append(kw) or kw
        )
        monkeypatch.setattr(mcp_server, "_identity", None)
        mcp_server.audit_log(action="send_email", resource="to=x", outcome="success")
        assert captured[0]["origin"] == "model"

    def test_system_events_are_marked_as_the_servers_and_reach_the_disk(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.tools import audit

        synced: list[int] = []
        monkeypatch.setattr(audit, "_audit_dir", lambda: tmp_path)
        monkeypatch.setattr(audit.os, "fsync", lambda fd: synced.append(fd))
        event = audit.log_event(action="a", resource="r", agent_id="agent")
        assert event["origin"] == "system"
        assert synced, "the event is fsynced before log_event returns"
        line = next(tmp_path.glob("*.jsonl")).read_text().strip()
        assert json.loads(line)["origin"] == "system"
