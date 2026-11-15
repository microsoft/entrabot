"""Registering the agent in the Agent 365 registry (Agent Registry API, Graph beta).

Microsoft Learn: "Creating a Microsoft Entra identity alone doesn't register the instance."
An agent whose Blueprint was made through the Graph API registers with
``POST /beta/copilot/agentRegistrations`` (``AgentRegistration.ReadWrite.All``). The API has
no list call, so the registration's ID is kept in the state file and is how a second run
finds it. A dry run is the default; nothing is written without ``--apply``.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "register_agent_instance.py"
URL = "https://graph.microsoft.com/beta/copilot/agentRegistrations"


def _jwt(claims: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


TOKEN = _jwt({"roles": ["AgentRegistration.ReadWrite.All", "Application.Read.All"]})
STATE = {
    "TENANT_ID": "tenant",
    "PROVISIONER_CLIENT_ID": "provisioner-app",
    "BLUEPRINT_APP_ID": "blueprint-app",
    "AGENT_ID": "agent-app",
    "AGENT_USER_UPN": "entrabot-agent-sati@contoso.test",
}


class Response:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture
def module(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("register_agent_instance", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    state = dict(STATE)
    monkeypatch.setattr(mod, "get_state", state.get)
    monkeypatch.setattr(mod, "set_state", state.__setitem__)
    monkeypatch.setattr(mod, "get_existing_graph_token", lambda: TOKEN)
    mod._test_state = state
    return mod


class Graph:
    def __init__(self, *replies: Response) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs.get("json")))
        return self.replies.pop(0)


def test_a_dry_run_writes_nothing_and_shows_the_payload(module, monkeypatch, capsys) -> None:
    graph = Graph()
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--owner-id", "owner-1"]) == 0
    assert graph.calls == []
    out = capsys.readouterr().out
    assert "dry run" in out.lower() and '"agentIdentityId": "agent-app"' in out
    assert "AGENT_REGISTRATION_ID" not in module._test_state


def test_apply_creates_a_registration_and_records_its_id(module, monkeypatch) -> None:
    graph = Graph(Response(201, {"id": "registration-1"}))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 0
    ((method, url, body),) = graph.calls
    assert (method, url) == ("POST", URL)
    assert body["agentIdentityId"] == "agent-app"
    assert body["agentIdentityBlueprintId"] == "blueprint-app"
    assert body["ownerIds"] == ["owner-1"]
    assert body["createdBy"] == "provisioner-app"
    for required in ("displayName", "sourceCreatedDateTime", "sourceLastModifiedDateTime"):
        assert body[required]
    assert module._test_state["AGENT_REGISTRATION_ID"] == "registration-1"


def test_a_recorded_registration_is_updated_not_duplicated(module, monkeypatch) -> None:
    module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
    graph = Graph(Response(200, {"id": "registration-1"}), Response(200))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 0
    assert [call[0] for call in graph.calls] == ["GET", "PATCH"]
    assert graph.calls[1][1] == f"{URL}/registration-1"
    assert "sourceCreatedDateTime" not in graph.calls[1][2]  # not updatable


def test_a_recorded_registration_that_is_gone_is_created_again(module, monkeypatch) -> None:
    module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
    graph = Graph(Response(404), Response(201, {"id": "registration-2"}))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 0
    assert [call[0] for call in graph.calls] == ["GET", "POST"]
    assert module._test_state["AGENT_REGISTRATION_ID"] == "registration-2"


def test_a_lookup_that_fails_creates_nothing(module, monkeypatch) -> None:
    # "Could not check" is not "there is none": a second registration is not created on it.
    module._test_state["AGENT_REGISTRATION_ID"] = "registration-1"
    graph = Graph(Response(503))
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 1
    assert [call[0] for call in graph.calls] == ["GET"]


def test_without_the_permission_it_stops_before_any_call(module, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "get_existing_graph_token", lambda: _jwt({"roles": []}))
    graph = Graph()
    monkeypatch.setattr(module, "_request", graph)
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 1
    assert graph.calls == []
    assert "AgentRegistration.ReadWrite.All" in capsys.readouterr().err


def test_an_owner_is_required(module, monkeypatch, capsys) -> None:
    # App-only registration needs owners, and they must be users (Learn, bulk onboarding).
    monkeypatch.delenv("ENTRABOT_HUMAN_USER_ID", raising=False)
    monkeypatch.setattr(module, "_request", Graph())
    assert module.main(["--apply"]) == 2
    assert "owner" in capsys.readouterr().err.lower()


def test_a_missing_agent_id_stops(module, monkeypatch) -> None:
    del module._test_state["AGENT_ID"]
    monkeypatch.setattr(module, "_request", Graph())
    assert module.main(["--apply", "--owner-id", "owner-1"]) == 2


def test_no_token_is_printed(module, monkeypatch, capsys) -> None:
    monkeypatch.setattr(module, "_request", Graph(Response(201, {"id": "r"})))
    module.main(["--apply", "--owner-id", "owner-1"])
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
