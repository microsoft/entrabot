"""The agent's entry in the Agent 365 registry: the request body, and create / update / read /
delete against the Agent Registry API, with Graph faked."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from entrabot.a365 import registry

URL = "https://graph.microsoft.com/beta/copilot/agentRegistrations"
NOW = datetime(2026, 11, 15, 9, 30, 0, tzinfo=UTC)


def _jwt(claims: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class Response:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


class Graph:
    def __init__(self, *replies: Response) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs.get("json")))
        assert kwargs["headers"]["Authorization"] == "Bearer tok"
        return self.replies.pop(0)


def _body(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "agent_id": "agent-app",
        "blueprint_app_id": "blueprint-app",
        "name": "Sati",
        "owner_ids": ["owner-1"],
        "created_by": "provisioner-app",
        "now": NOW,
    }
    values.update(overrides)
    return registry.build_registration(**values)


class TestTheBody:
    def test_it_names_the_agent_its_blueprint_and_its_owners(self) -> None:
        body = _body()
        assert body["agentIdentityId"] == "agent-app"
        assert body["sourceAgentId"] == "agent-app"
        assert body["agentIdentityBlueprintId"] == "blueprint-app"
        assert body["ownerIds"] == ["owner-1"]
        assert body["createdBy"] == "provisioner-app"
        assert body["displayName"] == "Sati" and body["agentCard"]["name"] == "Sati"
        assert body["sourceCreatedDateTime"] == "2026-11-15T09:30:00Z"
        assert body["originatingStore"] == "Entrabot"

    def test_the_display_name_prefers_a_human_one_then_the_upn(self) -> None:
        assert registry.display_name(preferred=" Nemo ", upn="entrabot-agent-x@c.com") == "Nemo"
        assert registry.display_name(preferred="", upn="entrabot-agent-x@c.com") == (
            "entrabot-agent-x"
        )
        assert registry.display_name(preferred="", upn="") == "Entrabot agent"


class TestOwners:
    def test_members_among_the_sponsors_own_it(self) -> None:
        env = {
            "ENTRABOT_HUMAN_USER_IDS": "member-1,guest-1,member-2",
            "ENTRABOT_HUMAN_USER_TYPES": "Member,Guest,Member",
            "ENTRABOT_HUMAN_USER_ID": "member-1",
        }
        assert registry.owner_ids(env) == ["member-1", "member-2"]

    def test_guests_own_it_when_there_is_no_member(self) -> None:
        env = {"ENTRABOT_HUMAN_USER_IDS": "guest-1", "ENTRABOT_HUMAN_USER_TYPES": "Guest"}
        assert registry.owner_ids(env) == ["guest-1"]

    def test_the_single_sponsor_line_is_enough(self) -> None:
        assert registry.owner_ids({"ENTRABOT_HUMAN_USER_ID": "only"}) == ["only"]
        assert registry.owner_ids({}) == []


def test_roles_are_read_from_the_token_without_trusting_it() -> None:
    assert registry.token_roles(_jwt({"roles": ["A", "B"]})) == ["A", "B"]
    assert registry.token_roles(_jwt({})) == []
    assert registry.token_roles("not-a-token") == []


class TestRegister:
    def test_nothing_recorded_creates_one(self) -> None:
        graph = Graph(Response(201, {"id": "registration-1"}))
        assert registry.register("tok", _body(), recorded_id="", request=graph) == (
            "registration-1",
            "created",
        )
        assert graph.calls == [("POST", URL, _body())]

    def test_a_recorded_one_is_read_then_updated(self) -> None:
        graph = Graph(Response(200, {"id": "r1", "agentIdentityId": "AGENT-APP"}), Response(204))
        assert registry.register("tok", _body(), recorded_id="r1", request=graph) == (
            "r1",
            "updated",
        )
        assert [(m, u) for m, u, _ in graph.calls] == [("GET", f"{URL}/r1"), ("PATCH", f"{URL}/r1")]
        patch = graph.calls[1][2]
        assert "sourceCreatedDateTime" not in patch and "createdBy" not in patch
        assert patch["displayName"] == "Sati"

    def test_a_recorded_one_that_is_gone_is_created_again(self) -> None:
        graph = Graph(Response(404), Response(201, {"id": "r2"}))
        assert registry.register("tok", _body(), recorded_id="r1", request=graph) == (
            "r2",
            "recreated",
        )

    def test_a_lookup_that_fails_creates_nothing(self) -> None:
        graph = Graph(Response(503))
        with pytest.raises(registry.RegistryError) as caught:
            registry.register("tok", _body(), recorded_id="r1", request=graph)
        assert caught.value.outcome == "could-not-check"
        assert [m for m, _, _ in graph.calls] == ["GET"]

    def test_a_recorded_one_for_another_agent_is_not_touched(self) -> None:
        graph = Graph(Response(200, {"id": "r1", "agentIdentityId": "someone-else"}))
        with pytest.raises(registry.RegistryError) as caught:
            registry.register("tok", _body(), recorded_id="r1", request=graph)
        assert caught.value.outcome == "not-ours"
        assert [m for m, _, _ in graph.calls] == ["GET"]

    def test_graph_refusing_the_write_is_said_with_its_status(self) -> None:
        graph = Graph(Response(403, {"error": {"code": "Authorization_RequestDenied"}}))
        with pytest.raises(registry.RegistryError) as caught:
            registry.register("tok", _body(), recorded_id="", request=graph)
        assert caught.value.outcome == "refused"
        assert "403" in str(caught.value)

    def test_a_refused_update_is_refused(self) -> None:
        graph = Graph(Response(200, {"id": "r1"}), Response(400, {"error": "bad"}))
        with pytest.raises(registry.RegistryError) as caught:
            registry.register("tok", _body(), recorded_id="r1", request=graph)
        assert caught.value.outcome == "refused"

    def test_a_created_registration_without_an_id_is_an_error(self) -> None:
        graph = Graph(Response(201, {}))
        with pytest.raises(registry.RegistryError) as caught:
            registry.register("tok", _body(), recorded_id="", request=graph)
        assert caught.value.outcome == "no-id"


class TestLookUpAndDelete:
    def test_look_up(self) -> None:
        assert registry.look_up("tok", "r1", request=Graph(Response(200, {"id": "r1"}))) == (
            "present"
        )
        assert registry.look_up("tok", "r1", request=Graph(Response(404))) == "gone"
        with pytest.raises(registry.RegistryError) as caught:
            registry.look_up("tok", "r1", request=Graph(Response(403)))
        assert caught.value.outcome == "could-not-check"

    def test_delete(self) -> None:
        graph = Graph(Response(204))
        assert registry.delete("tok", "r1", request=graph) == "deleted"
        assert graph.calls == [("DELETE", f"{URL}/r1", None)]
        assert registry.delete("tok", "r1", request=Graph(Response(404))) == "gone"
        with pytest.raises(registry.RegistryError) as caught:
            registry.delete("tok", "r1", request=Graph(Response(403)))
        assert caught.value.outcome == "refused"

    def test_an_id_is_never_spliced_into_another_path(self) -> None:
        with pytest.raises(registry.RegistryError):
            registry.look_up("tok", "../users", request=Graph())
