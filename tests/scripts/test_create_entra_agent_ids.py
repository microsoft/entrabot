"""Tests for the Blueprint-scoped lookup logic in create_entra_agent_ids.py.

The wire fix being guarded: when a tenant hosts multiple EntraBot
Blueprints, every Blueprint gets its own Agent Identity SP with the
same display name (``EntraBot Agent - <host>``). A lookup that
filters only on displayName can return the wrong Blueprint's
identity. These tests pin the fix: both ``find_existing_agent_identity``
and ``find_existing_agent_user`` must scope their results to the
intended Blueprint.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "create_entra_agent_ids.py"


class TestCurrentGraphCreationEndpoints:
    def test_creates_blueprint_principal_with_v1_subtype_endpoint(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, dict | None, str | None]] = []

        def fake_graph_request(method, path, token, **kwargs):
            calls.append((method, path, kwargs.get("json_body"), kwargs.get("base_url")))
            if method == "GET":
                return _resp(200, {"value": []})
            return _resp(201, {"id": "blueprint-principal-id"})

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        agent_ids_module.ensure_blueprint_principal("tok", "blueprint-app-id")

        assert calls[1] == (
            "POST",
            "/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal",
            {"appId": "blueprint-app-id"},
            agent_ids_module.GRAPH_V1,
        )

    def test_creates_blueprint_with_v1_subtype_endpoint(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, dict | None, str | None]] = []

        def fake_graph_request(method, path, token, **kwargs):
            calls.append((method, path, kwargs.get("json_body"), kwargs.get("base_url")))
            return _resp(201, {"appId": "blueprint-app-id", "id": "blueprint-object-id"})

        monkeypatch.setattr(agent_ids_module, "_FORCE_NEW", True)
        monkeypatch.setattr(
            agent_ids_module,
            "build_sponsors_bind",
            lambda: ["https://graph.microsoft.com/v1.0/users/sponsor-id"],
        )
        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(agent_ids_module, "set_state", lambda *args: None)
        monkeypatch.setattr(agent_ids_module, "ensure_blueprint_principal", lambda *args: None)

        agent_ids_module.create_blueprint("tok")

        assert calls == [
            (
                "POST",
                "/applications/microsoft.graph.agentIdentityBlueprint",
                {
                    "displayName": agent_ids_module.BLUEPRINT_DISPLAY_NAME,
                    "description": "Agent Identity Blueprint for EntraBot device agents",
                    "sponsors@odata.bind": [
                        "https://graph.microsoft.com/v1.0/users/sponsor-id"
                    ],
                },
                agent_ids_module.GRAPH_V1,
            )
        ]

    def test_creates_agent_identity_with_v1_subtype_endpoint(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, dict | None, str | None]] = []

        def fake_graph_request(method, path, token, **kwargs):
            calls.append((method, path, kwargs.get("json_body"), kwargs.get("base_url")))
            return _resp(201, {"appId": "agent-app-id", "id": "agent-object-id"})

        monkeypatch.setattr(agent_ids_module, "_FORCE_NEW", True)
        monkeypatch.setattr(agent_ids_module, "_agent_display_name", lambda: DISPLAY_NAME)
        monkeypatch.setattr(agent_ids_module, "get_signed_in_user_id", lambda: "sponsor-id")
        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(agent_ids_module, "set_state", lambda *args: None)

        agent_ids_module.create_agent_identity("tok", BLUEPRINT_OURS)

        assert calls == [
            (
                "POST",
                "/servicePrincipals/microsoft.graph.agentIdentity",
                {
                    "displayName": DISPLAY_NAME,
                    "agentIdentityBlueprintId": BLUEPRINT_OURS,
                    "sponsors@odata.bind": [
                        "https://graph.microsoft.com/v1.0/users/sponsor-id"
                    ],
                },
                agent_ids_module.GRAPH_V1,
            )
        ]


@pytest.fixture
def agent_ids_module():
    spec = importlib.util.spec_from_file_location("create_entra_agent_ids", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["create_entra_agent_ids"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("create_entra_agent_ids", None)


def _resp(status: int, body: dict) -> SimpleNamespace:
    """Build a minimal object that quacks like requests.Response."""
    return SimpleNamespace(status_code=status, json=lambda: body, text=str(body))


BLUEPRINT_OURS = "9bfb75b3-e65f-4e56-bdbe-3ed213135c3b"
BLUEPRINT_OTHER = "11111111-1111-1111-1111-111111111111"
DISPLAY_NAME = "EntraBot Agent - test-host"


def _sp(app_id: str, blueprint: str) -> dict:
    return {
        "id": app_id,
        "appId": app_id,
        "displayName": DISPLAY_NAME,
        "agentIdentityBlueprintId": blueprint,
        "@odata.type": "#microsoft.graph.agentIdentity",
    }


class TestFindExistingAgentIdentity:
    def test_returns_sp_matching_target_blueprint(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two SPs share a display name — only the one under our Blueprint is returned."""

        def fake_graph_request(method, path, token, **kw):
            return _resp(
                200,
                {
                    "value": [
                        _sp("eba51655-0aed-4a79-a5f2-7167ec9b8fa0", BLUEPRINT_OURS),
                        _sp("22222222-2222-2222-2222-222222222222", BLUEPRINT_OTHER),
                    ]
                },
            )

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        result = agent_ids_module.find_existing_agent_identity(
            token="tok",
            display_name=DISPLAY_NAME,
            blueprint_app_id=BLUEPRINT_OURS,
        )
        assert result is not None
        assert result["appId"] == "eba51655-0aed-4a79-a5f2-7167ec9b8fa0"
        assert result["agentIdentityBlueprintId"] == BLUEPRINT_OURS

    def test_rejects_stored_app_id_from_other_blueprint(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """stored_app_id from a different Blueprint must not be trusted.

        Regression for the 2026-04-19 cross-contamination: state held an
        AGENT_ID from an old Blueprint and the lookup returned it
        verbatim, silently pinning the chain to the wrong Blueprint.
        """

        def fake_graph_request(method, path, token, **kw):
            # stored_app_id query returns the old-Blueprint SP
            if "eq '22222222" in path:
                return _resp(
                    200,
                    {
                        "value": [
                            _sp("22222222-2222-2222-2222-222222222222", BLUEPRINT_OTHER),
                        ]
                    },
                )
            # displayName fallback returns nothing under our Blueprint
            return _resp(200, {"value": []})

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        result = agent_ids_module.find_existing_agent_identity(
            token="tok",
            display_name=DISPLAY_NAME,
            blueprint_app_id=BLUEPRINT_OURS,
            stored_app_id="22222222-2222-2222-2222-222222222222",
        )
        assert result is None
        # Warning printed so the operator sees why state got rejected
        assert "parented by a different Blueprint" in capsys.readouterr().out

    def test_returns_none_when_no_sp_under_our_blueprint(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_graph_request(method, path, token, **kw):
            return _resp(
                200,
                {
                    "value": [
                        _sp("22222222-2222-2222-2222-222222222222", BLUEPRINT_OTHER),
                    ]
                },
            )

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        result = agent_ids_module.find_existing_agent_identity(
            token="tok",
            display_name=DISPLAY_NAME,
            blueprint_app_id=BLUEPRINT_OURS,
        )
        assert result is None


class TestFindExistingAgentUser:
    _OUR_AI = "eba51655-0aed-4a79-a5f2-7167ec9b8fa0"
    _OTHER_AI = "22222222-2222-2222-2222-222222222222"

    def test_stored_user_under_our_agent_identity_is_trusted(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_graph_request(method, path, token, **kw):
            return _resp(
                200,
                {
                    "id": "aaaabbbb-cccc-dddd-eeee-111122223333",
                    "userPrincipalName": "entrabot-agent-sati-agent@fabrikam.onmicrosoft.com",
                    "identityParentId": self._OUR_AI,
                },
            )

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(
            agent_ids_module,
            "get_state",
            lambda k: "aaaabbbb-cccc-dddd-eeee-111122223333" if k == "AGENT_USER_ID" else None,
        )

        result = agent_ids_module.find_existing_agent_user(
            token="tok",
            agent_identity_obj_id=self._OUR_AI,
        )
        assert result is not None
        assert result["identityParentId"] == self._OUR_AI

    def test_stored_user_under_other_agent_identity_is_rejected(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A stored AGENT_USER_ID whose user is parented by a different
        Agent Identity must not be returned — otherwise callers derive
        the wrong chain downstream."""
        calls: list[str] = []

        def fake_graph_request(method, path, token, **kw):
            calls.append(path)
            if path.startswith("/users/aaaabbbb"):
                # Fetched stored user — but it's under a DIFFERENT Agent Identity
                return _resp(
                    200,
                    {
                        "id": "aaaabbbb-cccc-dddd-eeee-111122223333",
                        "identityParentId": self._OTHER_AI,
                    },
                )
            # Fallback identityParentId filter finds nothing under OUR AI
            return _resp(200, {"value": []})

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(
            agent_ids_module,
            "get_state",
            lambda k: "aaaabbbb-cccc-dddd-eeee-111122223333" if k == "AGENT_USER_ID" else None,
        )

        result = agent_ids_module.find_existing_agent_user(
            token="tok",
            agent_identity_obj_id=self._OUR_AI,
        )
        assert result is None
        assert "parented by a different Agent Identity" in capsys.readouterr().out
        # Verify both the stored lookup AND the fallback filter ran
        assert any(p.startswith("/users/aaaabbbb") for p in calls)
        assert any("identityParentId eq" in p for p in calls)


class TestAssignLicenseToAgentUser:
    def test_default_does_not_assign_copilot_when_teams_license_already_exists(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def fake_graph_request(method, path, token, **kw):
            calls.append((method, path, kw.get("json_body")))
            if path == "/users/agent-user-id?$select=assignedLicenses":
                return _resp(200, {"assignedLicenses": [{"skuId": "teams-sku"}]})
            if path == "/subscribedSkus":
                return _resp(
                    200,
                    {
                        "value": [
                            {
                                "skuId": "teams-sku",
                                "skuPartNumber": "SPE_E3",
                                "prepaidUnits": {"enabled": 5},
                                "consumedUnits": 1,
                            },
                            {
                                "skuId": "copilot-sku",
                                "skuPartNumber": "MICROSOFT_365_COPILOT",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 0,
                            },
                        ]
                    },
                )
            if path == "/users/agent-user-id":
                return _resp(204, {})
            if path == "/users/agent-user-id/assignLicense":
                return _resp(200, {})
            raise AssertionError(f"unexpected Graph call: {method} {path}")

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(agent_ids_module, "set_state", lambda *args: None)

        agent_ids_module.assign_license_to_agent_user("token", "agent-user-id")

        assign_calls = [call for call in calls if call[1] == "/users/agent-user-id/assignLicense"]
        assert assign_calls == []
        output = capsys.readouterr().out
        assert "already has Teams-capable license: SPE_E3" in output
        assert "Work IQ license assigned" not in output

    def test_assigns_copilot_when_work_iq_requested_and_teams_license_exists(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def fake_graph_request(method, path, token, **kw):
            calls.append((method, path, kw.get("json_body")))
            if path == "/users/agent-user-id?$select=assignedLicenses":
                return _resp(200, {"assignedLicenses": [{"skuId": "teams-sku"}]})
            if path == "/subscribedSkus":
                return _resp(
                    200,
                    {
                        "value": [
                            {
                                "skuId": "teams-sku",
                                "skuPartNumber": "SPE_E3",
                                "prepaidUnits": {"enabled": 5},
                                "consumedUnits": 1,
                            },
                            {
                                "skuId": "copilot-sku",
                                "skuPartNumber": "MICROSOFT_365_COPILOT",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 0,
                            },
                        ]
                    },
                )
            if path == "/users/agent-user-id":
                return _resp(204, {})
            if path == "/users/agent-user-id/assignLicense":
                return _resp(200, {})
            raise AssertionError(f"unexpected Graph call: {method} {path}")

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(agent_ids_module, "set_state", lambda *args: None)

        agent_ids_module.assign_license_to_agent_user(
            "token",
            "agent-user-id",
            assign_work_iq=True,
        )

        assign_calls = [call for call in calls if call[1] == "/users/agent-user-id/assignLicense"]
        assert assign_calls == [
            (
                "POST",
                "/users/agent-user-id/assignLicense",
                {"addLicenses": [{"skuId": "copilot-sku"}], "removeLicenses": []},
            )
        ]
        output = capsys.readouterr().out
        assert "already has Teams-capable license: SPE_E3" in output
        assert "Work IQ license assigned: MICROSOFT_365_COPILOT" in output

    def test_skips_when_agent_user_already_has_teams_and_copilot(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls: list[tuple[str, str]] = []

        def fake_graph_request(method, path, token, **kw):
            calls.append((method, path))
            if path == "/users/agent-user-id?$select=assignedLicenses":
                return _resp(
                    200,
                    {
                        "assignedLicenses": [
                            {"skuId": "teams-sku"},
                            {"skuId": "copilot-sku"},
                        ]
                    },
                )
            if path == "/subscribedSkus":
                return _resp(
                    200,
                    {
                        "value": [
                            {
                                "skuId": "teams-sku",
                                "skuPartNumber": "SPE_E3",
                                "prepaidUnits": {"enabled": 1},
                                "consumedUnits": 1,
                            },
                            {
                                "skuId": "copilot-sku",
                                "skuPartNumber": "MICROSOFT_365_COPILOT",
                                "prepaidUnits": {"enabled": 1},
                                "consumedUnits": 1,
                            },
                        ]
                    },
                )
            raise AssertionError(f"unexpected Graph call: {method} {path}")

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        agent_ids_module.assign_license_to_agent_user(
            "token",
            "agent-user-id",
            assign_work_iq=True,
        )

        assert not any(path.endswith("/assignLicense") for _, path in calls)
        assert "[skip] Agent User already has Teams and Work IQ licenses" in capsys.readouterr().out

    def test_does_not_reassign_when_existing_license_names_cannot_be_resolved(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls: list[tuple[str, str]] = []

        def fake_graph_request(method, path, token, **kw):
            calls.append((method, path))
            if path == "/users/agent-user-id?$select=assignedLicenses":
                return _resp(
                    200,
                    {
                        "assignedLicenses": [
                            {"skuId": "existing-teams-sku"},
                            {"skuId": "existing-copilot-sku"},
                        ]
                    },
                )
            if path == "/subscribedSkus":
                if calls.count(("GET", "/subscribedSkus")) == 1:
                    return _resp(500, {"error": "temporary_failure"})
                return _resp(
                    200,
                    {
                        "value": [
                            {
                                "skuId": "new-teams-sku",
                                "skuPartNumber": "SPE_E3",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 1,
                            },
                            {
                                "skuId": "new-copilot-sku",
                                "skuPartNumber": "MICROSOFT_365_COPILOT",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 1,
                            },
                        ]
                    },
                )
            raise AssertionError(f"unexpected Graph call: {method} {path}")

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        agent_ids_module.assign_license_to_agent_user(
            "token",
            "agent-user-id",
            assign_work_iq=True,
        )

        assert not any(path.endswith("/assignLicense") for _, path in calls)
        output = capsys.readouterr().out
        assert "Could not resolve existing Agent User license SKU names" in output

    def test_reports_only_work_iq_delay_when_teams_assignment_fails(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls: list[tuple[str, str, dict | None]] = []
        state: dict[str, str] = {}

        def fake_graph_request(method, path, token, **kw):
            calls.append((method, path, kw.get("json_body")))
            if path == "/users/agent-user-id?$select=assignedLicenses":
                return _resp(200, {"assignedLicenses": []})
            if path == "/subscribedSkus":
                return _resp(
                    200,
                    {
                        "value": [
                            {
                                "skuId": "teams-sku",
                                "skuPartNumber": "SPE_E3",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 1,
                            },
                            {
                                "skuId": "copilot-sku",
                                "skuPartNumber": "MICROSOFT_365_COPILOT",
                                "prepaidUnits": {"enabled": 2},
                                "consumedUnits": 1,
                            },
                        ]
                    },
                )
            if path == "/users/agent-user-id":
                return _resp(204, {})
            if path == "/users/agent-user-id/assignLicense":
                sku_id = kw["json_body"]["addLicenses"][0]["skuId"]
                return _resp(400 if sku_id == "teams-sku" else 200, {})
            raise AssertionError(f"unexpected Graph call: {method} {path}")

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(
            agent_ids_module,
            "time",
            type("FakeTime", (), {"sleep": lambda *_: None}),
        )
        monkeypatch.setattr(agent_ids_module, "set_state", state.__setitem__)

        agent_ids_module.assign_license_to_agent_user(
            "token",
            "agent-user-id",
            assign_work_iq=True,
        )

        output = capsys.readouterr().out
        assert "Work IQ provisioning can take 10-15 minutes" in output
        assert "Teams/mailbox and Work IQ provisioning can take 10-15 minutes" not in output
        assert state == {"AGENT_USER_WORK_IQ_LICENSE_SKU": "MICROSOFT_365_COPILOT"}


# ---------------------------------------------------------------------------
# Which Blueprint a run is for
# ---------------------------------------------------------------------------

OURS_OBJECT = "aaaaaaaa-0000-4000-8000-00000000000a"
OTHER_OBJECT = "bbbbbbbb-0000-4000-8000-00000000000b"
BY_NAME = "/applications?$filter=displayName eq 'EntraBot Code Agent'"
CREATE_BLUEPRINT = "/applications/microsoft.graph.agentIdentityBlueprint"


def _by_app_id(app_id: str) -> str:
    return f"/applications?$filter=appId eq '{app_id}'"


def _principal(app_id: str) -> str:
    return f"/servicePrincipals?$filter=appId eq '{app_id}'"


def _blueprint(app_id: str, object_id: str, created: str = "2026-04-19T17:02:11Z") -> dict:
    return {
        "@odata.type": "#microsoft.graph.agentIdentityBlueprint",
        "id": object_id,
        "appId": app_id,
        "displayName": "EntraBot Code Agent",
        "createdDateTime": created,
    }


def _not_json(status: int) -> SimpleNamespace:
    """A response whose body is not JSON (a proxy's error page, a truncated answer)."""

    def broken() -> dict:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    return SimpleNamespace(status_code=status, json=broken, text="<html>gateway</html>")


class FakeGraph:
    """Stands in for ``graph_request``: answers only the ``(method, path)`` a test set up.

    Any other call fails the test, so a request the code must never make (the search by
    display name, a create) shows up as a failure rather than passing unnoticed.
    """

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], object] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, method: str, path: str, token: str, json_body: dict | None = None, **_: object
    ) -> object:
        self.calls.append((method, path))
        reply = self.answers.get((method, path))
        if reply is None:
            raise AssertionError(f"unexpected Graph call: {method} {path}")
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]


@dataclass
class Lookup:
    module: ModuleType
    state: dict[str, str]
    graph: FakeGraph

    def answer(self, path: str, reply: object, *, method: str = "GET") -> None:
        self.graph.answers[(method, path)] = reply


@pytest.fixture
def lookup(agent_ids_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Lookup:
    """The script with its world replaced: the state file is a dict, Graph answers only what
    the test sets up, and nothing can reach the network or the Azure CLI."""
    state: dict[str, str] = {}
    graph = FakeGraph()
    monkeypatch.setattr(agent_ids_module, "get_state", state.get)
    monkeypatch.setattr(agent_ids_module, "set_state", state.__setitem__)
    monkeypatch.setattr(agent_ids_module, "graph_request", graph)
    monkeypatch.setattr(agent_ids_module, "get_existing_graph_token", lambda: "tok")
    monkeypatch.setattr(agent_ids_module, "build_sponsors_bind", lambda: [])
    monkeypatch.setattr(agent_ids_module, "get_signed_in_user_id", lambda: None)
    monkeypatch.setattr(agent_ids_module, "_FORCE_NEW", False)
    monkeypatch.setenv("ENTRABOT_TENANT_ID", "tenant-under-test")

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(requests.Session, "request", no_network)
    return Lookup(agent_ids_module, state, graph)


def _stopped(lookup: Lookup) -> str:
    """Run the lookup, which must stop; return what it tells the operator."""
    with pytest.raises(lookup.module.BlueprintLookupError) as stopped:
        lookup.module.find_existing_blueprint("tok")
    return str(stopped.value)


class TestTheBlueprintThisRunIsFor:
    """The defect: when the Blueprint recorded in the state file (setup records there the one
    it is asked to join, with --use-blueprint or -UseBlueprint) was not returned by Graph, for
    any reason at all, the lookup fell back to the first application named "EntraBot Code
    Agent". Display names are not unique, so this machine could be attached to somebody
    else's Blueprint without a word: its certificate registered there, its Agent Identity and
    Agent User resolved under it."""

    def test_a_recorded_blueprint_that_exists_is_the_one_used(self, lookup: Lookup) -> None:
        lookup.state["BLUEPRINT_APP_ID"] = BLUEPRINT_OURS
        lookup.answer(
            _by_app_id(BLUEPRINT_OURS),
            _resp(200, {"value": [_blueprint(BLUEPRINT_OURS, OURS_OBJECT)]}),
        )
        lookup.answer(_principal(BLUEPRINT_OURS), _resp(200, {"value": [{"id": "bp-sp"}]}))

        assert lookup.module.create_blueprint("tok") == (BLUEPRINT_OURS, OURS_OBJECT)

        assert lookup.state["BLUEPRINT_OBJECT_ID"] == OURS_OBJECT
        assert ("GET", BY_NAME) not in lookup.graph.calls
        assert lookup.graph.writes == []

    def test_the_recorded_app_id_in_other_letter_case_is_the_same_blueprint(
        self, lookup: Lookup
    ) -> None:
        lookup.state["BLUEPRINT_APP_ID"] = BLUEPRINT_OURS.upper()  # as typed
        lookup.answer(
            _by_app_id(BLUEPRINT_OURS.upper()),
            _resp(200, {"value": [_blueprint(BLUEPRINT_OURS, OURS_OBJECT)]}),  # as Graph has it
        )

        found = lookup.module.find_existing_blueprint("tok")

        assert found is not None and found["appId"] == BLUEPRINT_OURS

    def test_an_object_id_left_from_another_blueprint_cannot_pin_the_run_to_it(
        self, lookup: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The stored object ID used to be tried first and won outright: an object ID left over
        # from another Blueprint pinned the run to that Blueprint (scripts/setup.sh wipes the
        # state on a switch for exactly this reason). The App ID is what was asked for.
        lookup.state.update(BLUEPRINT_APP_ID=BLUEPRINT_OURS, BLUEPRINT_OBJECT_ID=OTHER_OBJECT)
        lookup.answer(
            f"/applications/{OTHER_OBJECT}", _resp(200, _blueprint(BLUEPRINT_OTHER, OTHER_OBJECT))
        )
        lookup.answer(
            _by_app_id(BLUEPRINT_OURS),
            _resp(200, {"value": [_blueprint(BLUEPRINT_OURS, OURS_OBJECT)]}),
        )

        found = lookup.module.find_existing_blueprint("tok")

        assert found is not None
        assert (found["appId"], found["id"]) == (BLUEPRINT_OURS, OURS_OBJECT)
        assert OTHER_OBJECT in capsys.readouterr().out  # the stale record is reported

    def test_a_recorded_app_id_that_does_not_exist_stops_without_a_name_search(
        self, lookup: Lookup
    ) -> None:
        lookup.state["BLUEPRINT_APP_ID"] = BLUEPRINT_OURS
        lookup.answer(_by_app_id(BLUEPRINT_OURS), _resp(200, {"value": []}))
        # What the old fallback would have taken: another Blueprint with the same name.
        lookup.answer(BY_NAME, _resp(200, {"value": [_blueprint(BLUEPRINT_OTHER, OTHER_OBJECT)]}))

        message = _stopped(lookup)

        assert f"Blueprint {BLUEPRINT_OURS} does not exist in this tenant" in message
        assert "tenant-under-test" in message
        assert "mistyped" in message and "different tenant" in message and "deleted" in message
        assert "Nothing was created or changed" in message
        assert "--use-blueprint=APP_ID" in message and "-UseBlueprint APP_ID" in message
        assert BLUEPRINT_OTHER not in message
        assert lookup.graph.calls == [("GET", _by_app_id(BLUEPRINT_OURS))]

    def test_a_recorded_object_id_that_graph_says_is_gone_stops_without_a_name_search(
        self, lookup: Lookup
    ) -> None:
        lookup.state["BLUEPRINT_OBJECT_ID"] = OURS_OBJECT
        lookup.answer(
            f"/applications/{OURS_OBJECT}",
            _resp(404, {"error": {"code": "Request_ResourceNotFound"}}),
        )
        lookup.answer(BY_NAME, _resp(200, {"value": [_blueprint(BLUEPRINT_OTHER, OTHER_OBJECT)]}))

        message = _stopped(lookup)

        assert OURS_OBJECT in message and "does not exist in this tenant" in message
        assert ("GET", BY_NAME) not in lookup.graph.calls

    @pytest.mark.parametrize(
        ("reply", "reason"),
        [
            (
                _resp(500, {"error": {"code": "InternalServerError"}}),
                "HTTP 500 (InternalServerError)",
            ),
            (_resp(503, {}), "HTTP 503"),
            (_resp(429, {"error": {"code": "TooManyRequests"}}), "HTTP 429 (TooManyRequests)"),
            (
                _resp(403, {"error": {"code": "Authorization_RequestDenied"}}),
                "HTTP 403 (Authorization_RequestDenied)",
            ),
            (_resp(401, {}), "HTTP 401"),
            # A collection query answers "none" with an empty list; a 404 from it says nothing
            # about this App ID.
            (_resp(404, {}), "HTTP 404"),
            (requests.exceptions.ReadTimeout("read timed out"), "ReadTimeout"),
            (requests.exceptions.ConnectionError("no route to host"), "ConnectionError"),
            (_not_json(200), "could not be read"),
            (_resp(200, {"unexpected": "shape"}), "could not be read"),
            (_resp(200, {"value": [{"appId": BLUEPRINT_OURS}]}), "could not be read"),  # no id
        ],
        ids=[
            "500",
            "503",
            "throttled",
            "forbidden",
            "unauthorized",
            "404-from-the-query",
            "timeout",
            "network",
            "not-json",
            "no-value-list",
            "no-object-id",
        ],
    )
    def test_a_recorded_app_id_that_could_not_be_checked_stops_without_a_name_search(
        self, lookup: Lookup, reply: object, reason: str
    ) -> None:
        lookup.state["BLUEPRINT_APP_ID"] = BLUEPRINT_OURS
        lookup.answer(_by_app_id(BLUEPRINT_OURS), reply)
        lookup.answer(BY_NAME, _resp(200, {"value": [_blueprint(BLUEPRINT_OTHER, OTHER_OBJECT)]}))

        message = _stopped(lookup)

        assert f"Could not check whether Blueprint {BLUEPRINT_OURS} exists" in message
        assert reason in message
        assert "does not exist" not in message  # "could not check" is not "not found"
        assert "Nothing was created or changed" in message
        assert lookup.graph.calls == [("GET", _by_app_id(BLUEPRINT_OURS))]

    def test_a_recorded_object_id_that_could_not_be_checked_stops_too(self, lookup: Lookup) -> None:
        lookup.state["BLUEPRINT_OBJECT_ID"] = OURS_OBJECT
        lookup.answer(f"/applications/{OURS_OBJECT}", _resp(503, {}))
        lookup.answer(BY_NAME, _resp(200, {"value": [_blueprint(BLUEPRINT_OTHER, OTHER_OBJECT)]}))

        message = _stopped(lookup)

        assert "Could not check whether" in message and OURS_OBJECT in message
        assert ("GET", BY_NAME) not in lookup.graph.calls

    def test_a_recorded_value_that_is_not_an_id_stops_before_asking_graph(
        self, lookup: Lookup
    ) -> None:
        lookup.state["BLUEPRINT_APP_ID"] = "9bfb75b3-e65f-4e56-bdbe"  # cut short when pasted

        message = _stopped(lookup)

        assert "'9bfb75b3-e65f-4e56-bdbe'" in message and "is not an ID" in message
        assert lookup.graph.calls == []

    def test_with_nothing_recorded_the_one_blueprint_with_the_name_is_used(
        self, lookup: Lookup
    ) -> None:
        lookup.answer(BY_NAME, _resp(200, {"value": [_blueprint(BLUEPRINT_OURS, OURS_OBJECT)]}))

        found = lookup.module.find_existing_blueprint("tok")

        assert found is not None and found["appId"] == BLUEPRINT_OURS

    def test_with_nothing_recorded_and_none_with_the_name_a_blueprint_is_created(
        self, lookup: Lookup
    ) -> None:
        lookup.answer(BY_NAME, _resp(200, {"value": []}))
        lookup.answer(
            CREATE_BLUEPRINT, _resp(201, _blueprint(BLUEPRINT_OURS, OURS_OBJECT)), method="POST"
        )
        lookup.answer(_principal(BLUEPRINT_OURS), _resp(200, {"value": [{"id": "bp-sp"}]}))

        assert lookup.module.create_blueprint("tok") == (BLUEPRINT_OURS, OURS_OBJECT)
        assert lookup.state["BLUEPRINT_APP_ID"] == BLUEPRINT_OURS

    def test_with_nothing_recorded_two_blueprints_with_the_name_stop_and_are_listed(
        self, lookup: Lookup
    ) -> None:
        lookup.answer(
            BY_NAME,
            _resp(
                200,
                {
                    "value": [
                        _blueprint(BLUEPRINT_OTHER, OTHER_OBJECT, created="2026-05-03T11:22:33Z"),
                        _blueprint(BLUEPRINT_OURS, OURS_OBJECT, created="2026-04-19T17:02:11Z"),
                    ]
                },
            ),
        )

        message = _stopped(lookup)

        assert "2 applications in this tenant are named 'EntraBot Code Agent'" in message
        lines = message.splitlines()
        for app_id, created in (
            (BLUEPRINT_OURS, "2026-04-19T17:02:11Z"),
            (BLUEPRINT_OTHER, "2026-05-03T11:22:33Z"),
        ):
            assert any(app_id in line and created in line for line in lines), message
        assert message.index(BLUEPRINT_OURS) < message.index(BLUEPRINT_OTHER)  # oldest first
        assert "Nothing was created or changed" in message
        assert "--use-blueprint=APP_ID" in message and "-UseBlueprint APP_ID" in message
        assert lookup.graph.writes == []

    def test_a_name_search_longer_than_one_page_is_not_taken_as_complete(
        self, lookup: Lookup
    ) -> None:
        page = {
            "value": [_blueprint(BLUEPRINT_OURS, OURS_OBJECT)],
            "@odata.nextLink": "https://graph.microsoft.com/beta/applications?$skiptoken=x",
        }
        lookup.answer(BY_NAME, _resp(200, page))

        message = _stopped(lookup)

        assert "Could not look for an existing Blueprint named 'EntraBot Code Agent'" in message
        assert lookup.graph.calls == [("GET", BY_NAME)]

    @pytest.mark.parametrize(
        "reply",
        [_resp(503, {}), requests.exceptions.ReadTimeout("slow"), _not_json(200)],
        ids=["503", "timeout", "not-json"],
    )
    def test_with_nothing_recorded_a_failed_name_search_stops_instead_of_creating(
        self, lookup: Lookup, reply: object
    ) -> None:
        # The old code read any failure here as "there is none" and created a Blueprint.
        lookup.answer(BY_NAME, reply)

        with pytest.raises(lookup.module.BlueprintLookupError) as stopped:
            lookup.module.create_blueprint("tok")

        message = str(stopped.value)
        assert "Could not look for an existing Blueprint named 'EntraBot Code Agent'" in message
        assert "Nothing was created or changed" in message
        assert lookup.graph.writes == []

    def test_a_new_chain_run_looks_nothing_up_even_with_a_blueprint_recorded(
        self, lookup: Lookup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lookup.module, "_FORCE_NEW", True)
        lookup.state.update(BLUEPRINT_APP_ID=BLUEPRINT_OTHER, BLUEPRINT_OBJECT_ID=OTHER_OBJECT)
        lookup.answer(
            CREATE_BLUEPRINT, _resp(201, _blueprint(BLUEPRINT_OURS, OURS_OBJECT)), method="POST"
        )
        lookup.answer(_principal(BLUEPRINT_OURS), _resp(200, {"value": [{"id": "bp-sp"}]}))

        assert lookup.module.create_blueprint("tok") == (BLUEPRINT_OURS, OURS_OBJECT)

        assert [call for call in lookup.graph.calls if call[1].startswith("/applications")] == [
            ("POST", CREATE_BLUEPRINT)
        ]

    @pytest.mark.parametrize(
        ("recorded", "answers"),
        [
            ({"BLUEPRINT_APP_ID": BLUEPRINT_OURS}, {_by_app_id(BLUEPRINT_OURS): _resp(200, {})}),
            (
                {"BLUEPRINT_APP_ID": BLUEPRINT_OURS},
                {_by_app_id(BLUEPRINT_OURS): _resp(200, {"value": []})},
            ),
            (
                {"BLUEPRINT_APP_ID": BLUEPRINT_OURS},
                {_by_app_id(BLUEPRINT_OURS): requests.exceptions.ReadTimeout("slow")},
            ),
            ({"BLUEPRINT_APP_ID": "not-an-app-id"}, {}),
            (
                {},
                {
                    BY_NAME: _resp(
                        200,
                        {
                            "value": [
                                _blueprint(BLUEPRINT_OURS, OURS_OBJECT),
                                _blueprint(BLUEPRINT_OTHER, OTHER_OBJECT),
                            ]
                        },
                    )
                },
            ),
            ({}, {BY_NAME: _resp(500, {})}),
        ],
        ids=[
            "unreadable-answer",
            "not-found",
            "could-not-check",
            "not-an-id",
            "several-with-the-name",
            "name-search-failed",
        ],
    )
    def test_every_stop_exits_1_before_anything_is_created_or_recorded(
        self,
        lookup: Lookup,
        capsys: pytest.CaptureFixture[str],
        recorded: dict[str, str],
        answers: dict[str, object],
    ) -> None:
        lookup.state.update(recorded, TENANT_ID="tenant-under-test")
        before = dict(lookup.state)
        for path, reply in answers.items():
            lookup.answer(path, reply)

        assert lookup.module.main() == 1

        assert "ERROR:" in capsys.readouterr().out
        assert lookup.graph.writes == []  # nothing created or changed in the directory
        assert lookup.state == before  # nor in the state file
