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
import json
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


BLUEPRINT_OURS = "b1b1b1b1-0000-4000-8000-0000000000b1"
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
                        _sp("a1a1a1a1-0000-4000-8000-0000000000a1", BLUEPRINT_OURS),
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
        assert result["appId"] == "a1a1a1a1-0000-4000-8000-0000000000a1"
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
    _OUR_AI = "a1a1a1a1-0000-4000-8000-0000000000a1"
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


def _sku_with_plans(sku_id: str, part: str, plans: list[str], *, free: int = 2) -> dict:
    return {
        "skuId": sku_id,
        "skuPartNumber": part,
        "appliesTo": "User",
        "prepaidUnits": {"enabled": free + 1},
        "consumedUnits": 1,
        "servicePlans": [{"servicePlanName": name, "appliesTo": "User"} for name in plans],
    }


class TestTheTeamsLicenceIsRecognisedByWhatItContains:
    """The licence step used a list of part numbers. It counted "Microsoft 365 E5 Extra
    Features" (no Teams) as Teams, and missed Microsoft 365 Business Basic and Premium."""

    def _run(
        self,
        agent_ids_module: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        skus: list[dict],
        existing: list[str],
    ) -> tuple[list[str], dict[str, str]]:
        assigned: list[str] = []
        state: dict[str, str] = {}

        def fake_graph_request(method, path, token, **kw):
            if path == "/users/agent-user-id?$select=assignedLicenses":
                return _resp(200, {"assignedLicenses": [{"skuId": item} for item in existing]})
            if path == "/subscribedSkus":
                return _resp(200, {"value": skus})
            if path == "/users/agent-user-id":
                return _resp(204, {})
            if path == "/users/agent-user-id/assignLicense":
                assigned.append(kw["json_body"]["addLicenses"][0]["skuId"])
                return _resp(200, {})
            raise AssertionError(f"unexpected Graph call: {method} {path}")

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(agent_ids_module, "set_state", state.__setitem__)
        agent_ids_module.assign_license_to_agent_user("token", "agent-user-id")
        return assigned, state

    def test_an_agent_with_only_e5_extra_features_still_gets_teams(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skus = [
            _sku_with_plans("extra", "M365_E5_SUITE_COMPONENTS", ["CLIPCHAMP", "MICROSOFT_LOOP"]),
            _sku_with_plans("spb", "SPB", ["TEAMS1", "EXCHANGE_S_STANDARD", "SHAREPOINTSTANDARD"]),
        ]
        assigned, state = self._run(agent_ids_module, monkeypatch, skus, existing=["extra"])
        assert assigned == ["spb"] and state == {"AGENT_USER_LICENSE_SKU": "SPB"}

    def test_the_licence_that_also_brings_mail_and_files_is_chosen(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        skus = [
            _sku_with_plans("ess", "Teams_Ess", ["TeamsEss"]),
            _sku_with_plans("basic", "O365_BUSINESS_ESSENTIALS", ["TEAMS1", "EXCHANGE_S_STANDARD"]),
            _sku_with_plans("spb", "SPB", ["TEAMS1", "EXCHANGE_S_STANDARD", "SHAREPOINTSTANDARD"]),
        ]
        assigned, _state = self._run(agent_ids_module, monkeypatch, skus, existing=[])
        assert assigned == ["spb"]

    @pytest.mark.parametrize(
        "sku",
        [
            _sku_with_plans(
                "e3nt",
                "Microsoft_365_E3_(no_Teams)",
                ["MESH_AVATARS_FOR_TEAMS", "CDS_O365_P2", "EXCHANGE_S_ENTERPRISE"],
            ),
            _sku_with_plans("dev", "MCOCAP", ["TEAMS1", "MCOEV"]),
            _sku_with_plans("rooms", "Microsoft_Teams_Rooms_Pro", ["TEAMS1", "MCOEV"]),
        ],
        ids=["no-teams-suite", "shared-devices", "rooms"],
    )
    def test_a_licence_without_teams_for_a_person_is_never_assigned_as_one(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        sku: dict,
    ) -> None:
        assigned, state = self._run(agent_ids_module, monkeypatch, [sku], existing=[])
        assert assigned == [] and state == {}
        assert "No Teams-capable licenses found with available seats." in capsys.readouterr().out

    def test_an_agent_with_a_teams_licence_by_its_plans_is_left_alone(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        skus = [_sku_with_plans("basic", "SMB_BUSINESS_ESSENTIALS", ["TEAMS1"])]
        assigned, _state = self._run(agent_ids_module, monkeypatch, skus, existing=["basic"])
        assert assigned == []
        out = capsys.readouterr().out
        assert "already has Teams-capable license: SMB_BUSINESS_ESSENTIALS" in out


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
        lookup.state["BLUEPRINT_APP_ID"] = "b1b1b1b1-0000-4000-8000"  # cut short when pasted

        message = _stopped(lookup)

        assert "'b1b1b1b1-0000-4000-8000'" in message and "is not an ID" in message
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


# ---------------------------------------------------------------------------
# What a stop prints for the setup front ends
# ---------------------------------------------------------------------------

# The Windows setup wizard recognises exactly these lines (another change teaches it to), so they
# are pinned here as well as there.
MARKERS = {
    "BLUEPRINT_NOT_FOUND_MARKER": "ENTRABOT_RESULT=blueprint-not-found",
    "BLUEPRINT_COULD_NOT_CHECK_MARKER": "ENTRABOT_RESULT=blueprint-could-not-check",
    "BLUEPRINT_AMBIGUOUS_MARKER": "ENTRABOT_RESULT=blueprint-ambiguous",
    "AGENT_USER_WRONG_BLUEPRINT_MARKER": "ENTRABOT_RESULT=agent-user-wrong-blueprint",
    "AGENT_LOOKUP_COULD_NOT_CHECK_MARKER": "ENTRABOT_RESULT=agent-lookup-could-not-check",
}
BLUEPRINT_NOT_FOUND = MARKERS["BLUEPRINT_NOT_FOUND_MARKER"]
BLUEPRINT_COULD_NOT_CHECK = MARKERS["BLUEPRINT_COULD_NOT_CHECK_MARKER"]
BLUEPRINT_AMBIGUOUS = MARKERS["BLUEPRINT_AMBIGUOUS_MARKER"]
WRONG_BLUEPRINT = MARKERS["AGENT_USER_WRONG_BLUEPRINT_MARKER"]
AGENT_COULD_NOT_CHECK = MARKERS["AGENT_LOOKUP_COULD_NOT_CHECK_MARKER"]


def _the_one_marker(out: str) -> str:
    """The marker line a stop printed: exactly one, alone on its line, and the operator's
    message starts on the very next line."""
    lines = out.splitlines()
    marked = [line for line in lines if "ENTRABOT_RESULT=" in line]
    assert len(marked) == 1, out
    after = lines.index(marked[0]) + 1
    assert after < len(lines) and lines[after].startswith("  ERROR: "), out
    return marked[0]


class TestStopsSayWhichOutcomeItWas:
    def test_the_markers_are_the_lines_the_windows_wizard_matches(
        self, agent_ids_module: ModuleType
    ) -> None:
        from entrabot.winsetup.helpers import RESULT_MARKER

        for name, line in MARKERS.items():
            assert getattr(agent_ids_module, name) == line
            assert line.startswith(RESULT_MARKER)

    @pytest.mark.parametrize(
        ("recorded", "answers", "marker"),
        [
            (
                {"BLUEPRINT_APP_ID": BLUEPRINT_OURS},
                {_by_app_id(BLUEPRINT_OURS): _resp(200, {"value": []})},
                BLUEPRINT_NOT_FOUND,
            ),
            (
                {"BLUEPRINT_OBJECT_ID": OURS_OBJECT},
                {f"/applications/{OURS_OBJECT}": _resp(404, {"error": {"code": "NotFound"}})},
                BLUEPRINT_NOT_FOUND,
            ),
            # A recorded value that is not an ID can't name any Blueprint: same remedy as
            # not found (give the right App ID), and waiting would not help.
            ({"BLUEPRINT_APP_ID": "not-an-app-id"}, {}, BLUEPRINT_NOT_FOUND),
            (
                {"BLUEPRINT_APP_ID": BLUEPRINT_OURS},
                {_by_app_id(BLUEPRINT_OURS): _resp(503, {})},
                BLUEPRINT_COULD_NOT_CHECK,
            ),
            (
                {"BLUEPRINT_APP_ID": BLUEPRINT_OURS},
                {_by_app_id(BLUEPRINT_OURS): requests.exceptions.ReadTimeout("slow")},
                BLUEPRINT_COULD_NOT_CHECK,
            ),
            (
                {"BLUEPRINT_APP_ID": BLUEPRINT_OURS},
                {_by_app_id(BLUEPRINT_OURS): _resp(200, {})},
                BLUEPRINT_COULD_NOT_CHECK,
            ),
            (
                {"BLUEPRINT_OBJECT_ID": OURS_OBJECT},
                {f"/applications/{OURS_OBJECT}": _resp(503, {})},
                BLUEPRINT_COULD_NOT_CHECK,
            ),
            ({}, {BY_NAME: _resp(500, {})}, BLUEPRINT_COULD_NOT_CHECK),
            (
                {},
                {
                    BY_NAME: _resp(
                        200,
                        {
                            "value": [_blueprint(BLUEPRINT_OURS, OURS_OBJECT)],
                            "@odata.nextLink": "https://graph.microsoft.com/beta/applications?x",
                        },
                    )
                },
                BLUEPRINT_COULD_NOT_CHECK,
            ),
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
                BLUEPRINT_AMBIGUOUS,
            ),
        ],
        ids=[
            "app-id-not-found",
            "object-id-not-found",
            "not-an-id",
            "app-id-503",
            "app-id-timeout",
            "app-id-unreadable",
            "object-id-503",
            "name-search-failed",
            "name-search-paged",
            "several-with-the-name",
        ],
    )
    def test_every_blueprint_stop_prints_its_marker_once_right_before_the_message(
        self,
        lookup: Lookup,
        capsys: pytest.CaptureFixture[str],
        recorded: dict[str, str],
        answers: dict[str, object],
        marker: str,
    ) -> None:
        lookup.state.update(recorded)
        for path, reply in answers.items():
            lookup.answer(path, reply)

        assert lookup.module.main() == 1

        assert _the_one_marker(capsys.readouterr().out) == marker

    def test_a_run_that_completes_prints_no_marker(
        self, run: AgentRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": [_agent_user(OUR_AI_OBJECT)]}))
        run.answer(
            f"/servicePrincipals/{OUR_AI_OBJECT}",
            _resp(200, _identity(OUR_AI_OBJECT, OUR_AI_APP, BLUEPRINT_OURS)),
        )

        assert run.module.main() == 0

        assert "ENTRABOT_RESULT=" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Which Agent Identity and Agent User a run is for
# ---------------------------------------------------------------------------

AGENT_UPN = "entrabot-agent-sati@contoso.onmicrosoft.com"
OUR_AI_OBJECT = "cccccccc-0000-4000-8000-00000000000c"
OUR_AI_APP = "cccccccc-1111-4000-8000-00000000000c"
THEIR_AI_OBJECT = "dddddddd-0000-4000-8000-00000000000d"
THEIR_AI_APP = "dddddddd-1111-4000-8000-00000000000d"
AGENT_USER_OBJECT = "eeeeeeee-0000-4000-8000-00000000000e"
NEW_AI_OBJECT = "ffffffff-0000-4000-8000-00000000000f"
NEW_AI_APP = "ffffffff-1111-4000-8000-00000000000f"
NEW_USER_OBJECT = "abababab-0000-4000-8000-0000000000ab"
CREATE_IDENTITY = "/servicePrincipals/microsoft.graph.agentIdentity"
CREATE_PRINCIPAL = "/servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal"
CREATE_USER = "/users"
GRANTS = (
    "grant_agent_identity_app_permissions",
    "grant_agent_user_consent",
    "grant_agent_user_storage_consent",
    "assign_license_to_agent_user",
)


def _by_upn(upn: str) -> str:
    return (
        f"/users?$filter=userPrincipalName eq '{upn}'"
        "&$select=id,userPrincipalName,displayName,identityParentId,mailNickname,accountEnabled"
    )


def _identity_named(name: str) -> str:
    return f"/servicePrincipals?$filter=displayName eq '{name}'"


def _users_of(identity_object_id: str) -> str:
    return f"/users?$filter=identityParentId eq '{identity_object_id}'"


def _agent_user(parent: str | None = THEIR_AI_OBJECT) -> dict:
    user = {"id": AGENT_USER_OBJECT, "userPrincipalName": AGENT_UPN, "displayName": "EntraBot"}
    if parent is not None:
        user["identityParentId"] = parent
    return user


def _identity(object_id: str, app_id: str, blueprint: str) -> dict:
    return {
        "@odata.type": "#microsoft.graph.agentIdentity",
        "id": object_id,
        "appId": app_id,
        "displayName": DISPLAY_NAME,
        "agentIdentityBlueprintId": blueprint,
        "servicePrincipalType": "ServiceIdentity",
    }


@dataclass
class AgentRun:
    lookup: Lookup
    granted: list[tuple[str, ...]]

    @property
    def module(self) -> ModuleType:
        return self.lookup.module

    @property
    def state(self) -> dict[str, str]:
        return self.lookup.state

    @property
    def graph(self) -> FakeGraph:
        return self.lookup.graph

    def answer(self, path: str, reply: object, *, method: str = "GET") -> None:
        self.lookup.answer(path, reply, method=method)


def _agent_world(lookup: Lookup, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """What main() needs past the Blueprint, with nothing real behind it. The grants and the
    licence are recorded, not made (they would call Graph directly), and the Agent User's
    sign-in name is set here: importing the script loads this checkout's .env, which may name
    a real one."""
    granted: list[tuple[str, ...]] = []

    def recorder(name: str) -> object:
        def record(_token: str, *args: str, **_kwargs: object) -> None:
            granted.append((name, *args))

        return record

    for name in GRANTS:
        monkeypatch.setattr(lookup.module, name, recorder(name))
    monkeypatch.setattr(lookup.module, "_agent_display_name", lambda: DISPLAY_NAME)
    monkeypatch.setattr(lookup.module, "time", SimpleNamespace(sleep=lambda _seconds: None))
    monkeypatch.setenv("ENTRABOT_AGENT_USER_UPN", AGENT_UPN)
    monkeypatch.delenv("_ENTRABOT_UPN_SUFFIX", raising=False)
    return granted


@pytest.fixture
def run(lookup: Lookup, monkeypatch: pytest.MonkeyPatch) -> AgentRun:
    """main() on a machine joining Blueprint OURS, as setup.sh --use-blueprint leaves it: that
    App ID recorded, and an Agent User sign-in name in the environment."""
    granted = _agent_world(lookup, monkeypatch)
    lookup.state["BLUEPRINT_APP_ID"] = BLUEPRINT_OURS
    lookup.answer(
        _by_app_id(BLUEPRINT_OURS),
        _resp(200, {"value": [_blueprint(BLUEPRINT_OURS, OURS_OBJECT)]}),
    )
    lookup.answer(_principal(BLUEPRINT_OURS), _resp(200, {"value": [{"id": "bp-sp"}]}))
    return AgentRun(lookup, granted)


class TestAnAgentUserFoundByItsSignInName:
    """The defect: an Agent User found by its sign-in name was reused, with its parent Agent
    Identity, whatever Blueprint they belong to. setup.sh --use-blueprint=X keeps the previous
    agent's ENTRABOT_AGENT_USER_UPN in .env, and the script loads .env, so joining Blueprint X
    on a machine set up for another agent took that agent's Agent Identity and Agent User,
    granted consents, an app role and a licence on them, and recorded them next to X."""

    def test_one_of_another_blueprint_stops_the_run_before_anything_is_granted_or_recorded(
        self, run: AgentRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": [_agent_user(THEIR_AI_OBJECT)]}))
        run.answer(
            f"/servicePrincipals/{THEIR_AI_OBJECT}",
            _resp(200, _identity(THEIR_AI_OBJECT, THEIR_AI_APP, BLUEPRINT_OTHER)),
        )
        before = dict(run.state)

        assert run.module.main() == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == WRONG_BLUEPRINT
        message = out[out.index(WRONG_BLUEPRINT) :]
        # Names the Agent User, the Blueprint it belongs to and the one asked for ...
        assert AGENT_UPN in message
        assert BLUEPRINT_OTHER in message and BLUEPRINT_OURS in message
        assert "Nothing was created or changed" in message
        # ... and both ways on: join that Blueprint, or give this agent another name.
        assert f"--use-blueprint={BLUEPRINT_OTHER}" in message
        assert f"-UseBlueprint {BLUEPRINT_OTHER}" in message
        assert "--with-upn-suffix=NAME" in message and "-UpnSuffix NAME" in message
        assert run.granted == []
        assert run.graph.writes == []
        assert run.state == before  # not even this run's own Blueprint object ID

    @pytest.mark.parametrize(
        "blueprint_as_graph_has_it",
        [BLUEPRINT_OURS, BLUEPRINT_OURS.upper()],
        ids=["same", "other-letter-case"],
    )
    def test_one_of_this_blueprint_is_reused_as_before(
        self,
        run: AgentRun,
        capsys: pytest.CaptureFixture[str],
        blueprint_as_graph_has_it: str,
    ) -> None:
        run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": [_agent_user(OUR_AI_OBJECT)]}))
        run.answer(
            f"/servicePrincipals/{OUR_AI_OBJECT}",
            _resp(200, _identity(OUR_AI_OBJECT, OUR_AI_APP, blueprint_as_graph_has_it)),
        )

        assert run.module.main() == 0

        assert run.state == {
            "BLUEPRINT_APP_ID": BLUEPRINT_OURS,
            "BLUEPRINT_OBJECT_ID": OURS_OBJECT,
            "AGENT_ID": OUR_AI_APP,
            "AGENT_OBJECT_ID": OUR_AI_OBJECT,
            "AGENT_USER_ID": AGENT_USER_OBJECT,
            "AGENT_USER_UPN": AGENT_UPN,
        }
        assert run.granted == [
            ("grant_agent_identity_app_permissions", OUR_AI_OBJECT),
            ("grant_agent_user_consent", OUR_AI_OBJECT, AGENT_USER_OBJECT),
            ("grant_agent_user_storage_consent", OUR_AI_OBJECT, AGENT_USER_OBJECT),
            ("assign_license_to_agent_user", AGENT_USER_OBJECT),
        ]
        assert run.graph.writes == []  # the chain exists: nothing is created
        assert "Reusing existing Agent User" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "reply",
        [
            _resp(503, {}),
            _resp(403, {"error": {"code": "Authorization_RequestDenied"}}),
            requests.exceptions.ReadTimeout("read timed out"),
            _not_json(200),
            # The Agent Identity is there, but which Blueprint it belongs to is not.
            _resp(200, {"id": THEIR_AI_OBJECT, "appId": THEIR_AI_APP, "displayName": "x"}),
            _resp(
                200, {"id": THEIR_AI_OBJECT, "appId": THEIR_AI_APP, "agentIdentityBlueprintId": ""}
            ),
            # This run's Blueprint, but no App ID to record as the agent's.
            _resp(200, {"id": THEIR_AI_OBJECT, "agentIdentityBlueprintId": BLUEPRINT_OURS}),
            # Gone, or not listed yet: either way it says nothing about which Blueprint.
            _resp(404, {"error": {"code": "Request_ResourceNotFound"}}),
        ],
        ids=[
            "503",
            "forbidden",
            "timeout",
            "not-json",
            "no-blueprint-property",
            "empty-blueprint-property",
            "no-app-id",
            "parent-404",
        ],
    )
    def test_one_whose_blueprint_cannot_be_read_stops_as_could_not_check(
        self, run: AgentRun, capsys: pytest.CaptureFixture[str], reply: object
    ) -> None:
        run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": [_agent_user(THEIR_AI_OBJECT)]}))
        run.answer(f"/servicePrincipals/{THEIR_AI_OBJECT}", reply)
        before = dict(run.state)

        assert run.module.main() == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == AGENT_COULD_NOT_CHECK
        assert f"Could not check which Blueprint the Agent User {AGENT_UPN} belongs to" in out
        assert run.granted == []
        assert run.graph.writes == []
        assert run.state == before

    @pytest.mark.parametrize(
        "user",
        [_agent_user(parent=None), {**_agent_user(), "identityParentId": "not-an-object-id"}],
        ids=["no-parent", "parent-not-an-id"],
    )
    def test_one_that_names_no_agent_identity_stops_as_could_not_check(
        self, run: AgentRun, capsys: pytest.CaptureFixture[str], user: dict
    ) -> None:
        # An ordinary account can hold the name too. Nothing is asked about a parent it does
        # not name: an unexpected call fails the test.
        run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": [user]}))

        assert run.module.main() == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == AGENT_COULD_NOT_CHECK
        assert f"Could not check which Blueprint the Agent User {AGENT_UPN} belongs to" in out
        assert run.granted == [] and run.graph.writes == []

    @pytest.mark.parametrize(
        "reply",
        [
            _resp(503, {}),
            _resp(429, {"error": {"code": "TooManyRequests"}}),
            _resp(403, {"error": {"code": "Authorization_RequestDenied"}}),
            requests.exceptions.ConnectionError("no route to host"),
            _not_json(200),
            _resp(200, {"unexpected": "shape"}),
            _resp(200, {"value": [{"userPrincipalName": AGENT_UPN}]}),  # no object ID
        ],
        ids=["503", "throttled", "forbidden", "network", "not-json", "no-value-list", "no-id"],
    )
    def test_a_search_by_sign_in_name_that_fails_stops_before_anything_is_created(
        self, run: AgentRun, capsys: pytest.CaptureFixture[str], reply: object
    ) -> None:
        # The old code read a failure here as "no such user", went on to create a second
        # Agent Identity, and then collided on the name.
        run.answer(_by_upn(AGENT_UPN), reply)
        before = dict(run.state)

        assert run.module.main() == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == AGENT_COULD_NOT_CHECK
        assert f"Could not check whether an Agent User named {AGENT_UPN} exists" in out
        assert "does not exist" not in out  # "could not check" is not "not found"
        assert run.graph.calls == [("GET", _by_app_id(BLUEPRINT_OURS)), ("GET", _by_upn(AGENT_UPN))]
        assert run.granted == []
        assert run.state == before

    def test_with_none_of_that_name_the_agent_identity_and_user_are_created(
        self, run: AgentRun, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": []}))
        # This host's Agent Identity under another Blueprint has the same name: not ours.
        run.answer(
            _identity_named(DISPLAY_NAME),
            _resp(200, {"value": [_identity(THEIR_AI_OBJECT, THEIR_AI_APP, BLUEPRINT_OTHER)]}),
        )
        run.answer(
            CREATE_IDENTITY, _resp(201, {"appId": NEW_AI_APP, "id": NEW_AI_OBJECT}), method="POST"
        )
        run.answer(_users_of(NEW_AI_OBJECT), _resp(200, {"value": []}))
        run.answer(
            CREATE_USER,
            _resp(201, {"id": NEW_USER_OBJECT, "userPrincipalName": AGENT_UPN}),
            method="POST",
        )

        assert run.module.main() == 0

        assert run.graph.writes == [("POST", CREATE_IDENTITY), ("POST", CREATE_USER)]
        assert run.state["AGENT_OBJECT_ID"] == NEW_AI_OBJECT
        assert run.state["AGENT_USER_ID"] == NEW_USER_OBJECT
        assert ("grant_agent_user_consent", NEW_AI_OBJECT, NEW_USER_OBJECT) in run.granted
        assert "ENTRABOT_RESULT=" not in capsys.readouterr().out


def _identity_search_fails(run: AgentRun) -> None:
    run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": []}))
    run.answer(_identity_named(DISPLAY_NAME), _resp(503, {}))


def _recorded_identity_cannot_be_checked(run: AgentRun) -> None:
    run.state["AGENT_ID"] = OUR_AI_APP
    run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": []}))
    run.answer(_principal(OUR_AI_APP), requests.exceptions.ReadTimeout("slow"))


def _user_search_fails(run: AgentRun) -> None:
    run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": []}))
    run.answer(
        _identity_named(DISPLAY_NAME),
        _resp(200, {"value": [_identity(OUR_AI_OBJECT, OUR_AI_APP, BLUEPRINT_OURS)]}),
    )
    run.answer(_users_of(OUR_AI_OBJECT), _resp(400, {"error": {"code": "BadRequest"}}))


def _recorded_user_cannot_be_checked(run: AgentRun) -> None:
    run.state["AGENT_USER_ID"] = AGENT_USER_OBJECT
    run.answer(_by_upn(AGENT_UPN), _resp(200, {"value": []}))
    run.answer(
        _identity_named(DISPLAY_NAME),
        _resp(200, {"value": [_identity(OUR_AI_OBJECT, OUR_AI_APP, BLUEPRINT_OURS)]}),
    )
    run.answer(f"/users/{AGENT_USER_OBJECT}", _resp(503, {}))


class TestLookupsThatFailNeverReadAsNone:
    """The defect: find_existing_agent_identity and find_existing_agent_user took a failed
    Graph call for "there is none", and the run went on to create a second Agent Identity for
    this machine, or an Agent User that then collided."""

    @pytest.mark.parametrize(
        "setup",
        [
            _identity_search_fails,
            _recorded_identity_cannot_be_checked,
            _user_search_fails,
            _recorded_user_cannot_be_checked,
        ],
        ids=["identity-by-name", "recorded-identity", "user-by-identity", "recorded-user"],
    )
    def test_each_stops_before_anything_is_created_with_one_marker(
        self, run: AgentRun, capsys: pytest.CaptureFixture[str], setup: object
    ) -> None:
        setup(run)  # type: ignore[operator]

        assert run.module.main() == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == AGENT_COULD_NOT_CHECK
        assert "Could not check" in out
        assert run.graph.writes == []
        assert run.granted == []
        assert "AGENT_USER_UPN" not in run.state

    @pytest.mark.parametrize(
        "reply",
        [
            _resp(503, {}),
            requests.exceptions.ReadTimeout("slow"),
            _not_json(200),
            _resp(200, {"unexpected": "shape"}),
            # A match with no IDs could not be recorded.
            _resp(
                200,
                {
                    "value": [
                        {"displayName": DISPLAY_NAME, "agentIdentityBlueprintId": BLUEPRINT_OURS}
                    ]
                },
            ),
            # Only a whole list says there is none.
            _resp(
                200,
                {
                    "value": [_identity(THEIR_AI_OBJECT, THEIR_AI_APP, BLUEPRINT_OTHER)],
                    "@odata.nextLink": "https://graph.microsoft.com/beta/servicePrincipals?x",
                },
            ),
        ],
        ids=["503", "timeout", "not-json", "no-value-list", "no-ids", "more-pages"],
    )
    def test_an_agent_identity_search_that_fails_raises_instead_of_returning_none(
        self, lookup: Lookup, reply: object
    ) -> None:
        lookup.answer(_identity_named(DISPLAY_NAME), reply)

        with pytest.raises(lookup.module.AgentLookupError) as stopped:
            lookup.module.find_existing_agent_identity("tok", DISPLAY_NAME, BLUEPRINT_OURS)

        assert stopped.value.marker == AGENT_COULD_NOT_CHECK
        message = str(stopped.value)
        assert "Could not check whether this machine already has an Agent Identity" in message
        assert BLUEPRINT_OURS in message

    def test_a_recorded_agent_identity_that_could_not_be_checked_is_not_searched_past(
        self, lookup: Lookup
    ) -> None:
        lookup.answer(_principal(OUR_AI_APP), _resp(503, {}))
        lookup.answer(_identity_named(DISPLAY_NAME), _resp(200, {"value": []}))

        with pytest.raises(lookup.module.AgentLookupError) as stopped:
            lookup.module.find_existing_agent_identity(
                "tok", DISPLAY_NAME, BLUEPRINT_OURS, stored_app_id=OUR_AI_APP
            )

        assert OUR_AI_APP in str(stopped.value)
        assert lookup.graph.calls == [("GET", _principal(OUR_AI_APP))]

    def test_a_recorded_agent_identity_graph_says_is_gone_is_looked_for_by_name(
        self, lookup: Lookup
    ) -> None:
        lookup.answer(_principal(OUR_AI_APP), _resp(200, {"value": []}))
        lookup.answer(
            _identity_named(DISPLAY_NAME),
            _resp(200, {"value": [_identity(NEW_AI_OBJECT, NEW_AI_APP, BLUEPRINT_OURS)]}),
        )

        found = lookup.module.find_existing_agent_identity(
            "tok", DISPLAY_NAME, BLUEPRINT_OURS, stored_app_id=OUR_AI_APP
        )

        assert found is not None and found["id"] == NEW_AI_OBJECT

    def test_creating_an_agent_identity_stops_before_the_post(
        self, lookup: Lookup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lookup.module, "_agent_display_name", lambda: DISPLAY_NAME)
        lookup.answer(_identity_named(DISPLAY_NAME), _resp(503, {}))

        with pytest.raises(lookup.module.AgentLookupError):
            lookup.module.create_agent_identity("tok", BLUEPRINT_OURS)

        assert lookup.graph.writes == []
        assert lookup.state == {}

    @pytest.mark.parametrize(
        "reply",
        [_resp(503, {}), requests.exceptions.ReadTimeout("slow"), _not_json(200)],
        ids=["503", "timeout", "not-json"],
    )
    def test_a_recorded_agent_user_that_could_not_be_checked_raises(
        self, lookup: Lookup, reply: object
    ) -> None:
        lookup.state["AGENT_USER_ID"] = AGENT_USER_OBJECT
        lookup.answer(f"/users/{AGENT_USER_OBJECT}", reply)
        lookup.answer(_users_of(OUR_AI_OBJECT), _resp(200, {"value": []}))

        with pytest.raises(lookup.module.AgentLookupError) as stopped:
            lookup.module.find_existing_agent_user("tok", OUR_AI_OBJECT)

        assert stopped.value.marker == AGENT_COULD_NOT_CHECK
        assert AGENT_USER_OBJECT in str(stopped.value)
        assert lookup.graph.calls == [("GET", f"/users/{AGENT_USER_OBJECT}")]

    def test_a_recorded_agent_user_graph_says_is_gone_is_looked_for_by_its_agent_identity(
        self, lookup: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lookup.state["AGENT_USER_ID"] = AGENT_USER_OBJECT
        lookup.answer(f"/users/{AGENT_USER_OBJECT}", _resp(404, {"error": {"code": "NotFound"}}))
        lookup.answer(_users_of(OUR_AI_OBJECT), _resp(200, {"value": []}))

        assert lookup.module.find_existing_agent_user("tok", OUR_AI_OBJECT) is None
        assert "Stored AGENT_USER_ID not found" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "reply",
        [
            _resp(503, {}),
            _resp(400, {"error": {"code": "Request_UnsupportedQuery"}}),
            requests.exceptions.ConnectionError("down"),
            _not_json(200),
            _resp(200, {"value": [{"userPrincipalName": AGENT_UPN}]}),  # no object ID
        ],
        ids=["503", "unsupported-query", "network", "not-json", "no-id"],
    )
    def test_an_agent_user_search_that_fails_raises_instead_of_returning_none(
        self, lookup: Lookup, reply: object
    ) -> None:
        lookup.answer(_users_of(OUR_AI_OBJECT), reply)

        with pytest.raises(lookup.module.AgentLookupError) as stopped:
            lookup.module.find_existing_agent_user("tok", OUR_AI_OBJECT)

        assert stopped.value.marker == AGENT_COULD_NOT_CHECK
        assert (
            f"Could not check whether Agent Identity {OUR_AI_OBJECT} already has an Agent User"
            in str(stopped.value)
        )

    def test_creating_an_agent_user_stops_before_the_post(
        self, lookup: Lookup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENTRABOT_AGENT_USER_UPN", AGENT_UPN)
        monkeypatch.delenv("_ENTRABOT_UPN_SUFFIX", raising=False)
        lookup.answer(_users_of(OUR_AI_OBJECT), _resp(503, {}))

        with pytest.raises(lookup.module.AgentLookupError):
            lookup.module.create_agent_user("tok", OUR_AI_OBJECT)

        assert lookup.graph.writes == []
        assert lookup.state == {}

    def test_an_agent_user_search_that_finds_none_is_none(self, lookup: Lookup) -> None:
        lookup.answer(_users_of(OUR_AI_OBJECT), _resp(200, {"value": []}))

        assert lookup.module.find_existing_agent_user("tok", OUR_AI_OBJECT) is None

    def test_the_sign_in_name_matches_in_any_letter_case(self, lookup: Lookup) -> None:
        lookup.answer(_by_upn(AGENT_UPN.upper()), _resp(200, {"value": [_agent_user()]}))

        found = lookup.module.find_existing_agent_user_by_upn("tok", AGENT_UPN.upper())

        assert found is not None and found["id"] == AGENT_USER_OBJECT

    def test_no_user_with_the_sign_in_name_is_none(self, lookup: Lookup) -> None:
        lookup.answer(_by_upn(AGENT_UPN), _resp(200, {"value": []}))

        assert lookup.module.find_existing_agent_user_by_upn("tok", AGENT_UPN) is None


class TestANewChainLooksNothingUp:
    def test_everything_is_created_with_no_lookup_and_no_stop(
        self,
        lookup: Lookup,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        granted = _agent_world(lookup, monkeypatch)
        monkeypatch.setattr(lookup.module, "_FORCE_NEW", True)
        # What the previous agent left recorded: none of it is looked up.
        lookup.state.update(
            BLUEPRINT_APP_ID=BLUEPRINT_OTHER,
            AGENT_ID=THEIR_AI_APP,
            AGENT_USER_ID=AGENT_USER_OBJECT,
        )
        lookup.answer(
            CREATE_BLUEPRINT, _resp(201, _blueprint(BLUEPRINT_OURS, OURS_OBJECT)), method="POST"
        )
        lookup.answer(_principal(BLUEPRINT_OURS), _resp(200, {"value": []}))
        lookup.answer(CREATE_PRINCIPAL, _resp(201, {"id": "bp-sp"}), method="POST")
        lookup.answer(
            CREATE_IDENTITY, _resp(201, {"appId": NEW_AI_APP, "id": NEW_AI_OBJECT}), method="POST"
        )
        lookup.answer(
            CREATE_USER,
            _resp(201, {"id": NEW_USER_OBJECT, "userPrincipalName": AGENT_UPN}),
            method="POST",
        )

        assert lookup.module.main() == 0

        # The one read is the BlueprintPrincipal check that follows creating the Blueprint.
        assert [call for call in lookup.graph.calls if call[0] == "GET"] == [
            ("GET", _principal(BLUEPRINT_OURS))
        ]
        assert lookup.graph.writes == [
            ("POST", CREATE_BLUEPRINT),
            ("POST", CREATE_PRINCIPAL),
            ("POST", CREATE_IDENTITY),
            ("POST", CREATE_USER),
        ]
        assert ("assign_license_to_agent_user", NEW_USER_OBJECT) in granted
        assert "ENTRABOT_RESULT=" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The licence on its own: what the Windows wizard's licence walkthrough runs
# ---------------------------------------------------------------------------

LICENCE_MARKERS = {
    "LICENCE_ASSIGNED_MARKER": "ENTRABOT_RESULT=licence-assigned",
    "LICENCE_ALREADY_ASSIGNED_MARKER": "ENTRABOT_RESULT=licence-already-assigned",
    "LICENCE_NO_FREE_SEAT_MARKER": "ENTRABOT_RESULT=licence-no-free-seat",
    "LICENCE_NOT_FOUND_MARKER": "ENTRABOT_RESULT=licence-not-found",
    "LICENCE_ASSIGN_FAILED_MARKER": "ENTRABOT_RESULT=licence-assign-failed",
    "LICENCE_COULD_NOT_READ_MARKER": "ENTRABOT_RESULT=licence-could-not-read",
}
TEAMS_SKU = "11111111-aaaa-4000-8000-000000000001"
COPILOT_SKU = "22222222-aaaa-4000-8000-000000000002"
DEVICE_SKU = "33333333-aaaa-4000-8000-000000000003"
ASSIGNED_TO_AGENT = f"/users/{AGENT_USER_OBJECT}?$select=assignedLicenses"
BUSINESS_PREMIUM = _sku_with_plans(
    TEAMS_SKU, "SPB", ["TEAMS1", "EXCHANGE_S_STANDARD", "SHAREPOINTSTANDARD"], free=1
)
COPILOT = _sku_with_plans(COPILOT_SKU, "Microsoft_365_Copilot", ["M365_COPILOT_APPS"], free=1)
SHARED_DEVICES = _sku_with_plans(DEVICE_SKU, "MCOCAP", ["TEAMS1", "MCOEV"], free=4)


@pytest.fixture
def licences(lookup: Lookup, monkeypatch: pytest.MonkeyPatch) -> Lookup:
    """A machine whose agent exists: its Agent User is recorded, and no retry really waits."""
    lookup.state["AGENT_USER_ID"] = AGENT_USER_OBJECT
    monkeypatch.setattr(lookup.module, "time", SimpleNamespace(sleep=lambda _seconds: None))
    return lookup


def _result_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("ENTRABOT_RESULT=")]


class TestTheLicenceOnItsOwn:
    def test_the_markers_are_the_lines_the_windows_wizard_matches(
        self, agent_ids_module: ModuleType
    ) -> None:
        from entrabot.winsetup import licences

        for name, line in LICENCE_MARKERS.items():
            assert getattr(agent_ids_module, name) == line
        assert {line.split("=", 1)[1] for line in LICENCE_MARKERS.values()} == {
            licences.ASSIGNED,
            licences.ALREADY_ASSIGNED,
            licences.NO_FREE_SEAT,
            licences.NOT_FOUND,
            licences.ASSIGN_FAILED,
            licences.COULD_NOT_READ,
        }

    def test_the_report_says_what_the_tenant_and_the_agent_have_and_changes_nothing(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from entrabot.licensing import Report

        licences.answer("/subscribedSkus", _resp(200, {"value": [BUSINESS_PREMIUM, COPILOT]}))
        licences.answer(
            ASSIGNED_TO_AGENT, _resp(200, {"assignedLicenses": [{"skuId": COPILOT_SKU}]})
        )
        state = dict(licences.state)

        assert licences.module.main(["--licence-report"]) == 0

        (line,) = _result_lines(capsys.readouterr().out)
        report = Report.from_json(json.loads(line.split("=", 1)[1]))
        assert report.agent == (COPILOT_SKU,)
        assert [product.name for product in report.free_for("teams")] == [
            "Microsoft 365 Business Premium"
        ]
        assert report.agent_has("copilot") and not report.agent_has("teams")
        assert licences.graph.writes == [] and licences.state == state

    @pytest.mark.parametrize(
        "broken",
        ["/subscribedSkus", ASSIGNED_TO_AGENT],
    )
    def test_a_report_that_cannot_be_read_says_so(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str], broken: str
    ) -> None:
        licences.answer("/subscribedSkus", _resp(200, {"value": [BUSINESS_PREMIUM]}))
        licences.answer(ASSIGNED_TO_AGENT, _resp(200, {"assignedLicenses": []}))
        licences.answer(broken, _resp(403, {"error": {"code": "Authorization_RequestDenied"}}))

        assert licences.module.main(["--licence-report"]) == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == LICENCE_MARKERS["LICENCE_COULD_NOT_READ_MARKER"]
        assert "HTTP 403" in out

    def test_without_a_recorded_agent_user_nothing_is_asked(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del licences.state["AGENT_USER_ID"]

        assert licences.module.main(["--licence-report"]) == 1

        assert _the_one_marker(capsys.readouterr().out) == (
            LICENCE_MARKERS["LICENCE_COULD_NOT_READ_MARKER"]
        )
        assert licences.graph.calls == []

    def test_without_the_provisioner_token_nothing_is_asked(
        self,
        licences: Lookup,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def no_token() -> str:
            raise licences.module.ProvisionerBootstrapError("no Provisioner app is recorded")

        monkeypatch.setattr(licences.module, "get_existing_graph_token", no_token)

        assert licences.module.main(["--licence-report"]) == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == LICENCE_MARKERS["LICENCE_COULD_NOT_READ_MARKER"]
        assert "no Provisioner app is recorded" in out and licences.graph.calls == []

    def _assignable(self, licences: Lookup, *skus: dict, agent: list[str] | None = None) -> None:
        licences.answer("/subscribedSkus", _resp(200, {"value": list(skus)}))
        licences.answer(
            ASSIGNED_TO_AGENT,
            _resp(200, {"assignedLicenses": [{"skuId": item} for item in agent or []]}),
        )
        licences.answer(f"/users/{AGENT_USER_OBJECT}", _resp(204, {}), method="PATCH")
        licences.answer(
            f"/users/{AGENT_USER_OBJECT}/assignLicense", _resp(200, {}), method="POST"
        )

    def test_a_teams_licence_is_given_after_the_usage_location_and_recorded(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._assignable(licences, BUSINESS_PREMIUM, COPILOT)

        assert licences.module.main(["--assign-licence", TEAMS_SKU]) == 0

        assert _result_lines(capsys.readouterr().out) == [
            LICENCE_MARKERS["LICENCE_ASSIGNED_MARKER"]
        ]
        assert licences.graph.writes == [
            ("PATCH", f"/users/{AGENT_USER_OBJECT}"),
            ("POST", f"/users/{AGENT_USER_OBJECT}/assignLicense"),
        ]
        assert licences.state["AGENT_USER_LICENSE_SKU"] == "SPB"
        assert "AGENT_USER_WORK_IQ_LICENSE_SKU" not in licences.state

    def test_the_licence_given_is_the_one_asked_for(
        self, licences: Lookup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._assignable(licences, BUSINESS_PREMIUM, COPILOT)
        sent: list[dict] = []
        graph = licences.graph

        def recording(method: str, path: str, token: str, json_body=None, **kwargs):  # noqa: ANN001, ANN202
            if path.endswith("/assignLicense"):
                sent.append(json_body)
            return graph(method, path, token, json_body, **kwargs)

        monkeypatch.setattr(licences.module, "graph_request", recording)

        assert licences.module.main(["--assign-licence", COPILOT_SKU]) == 0

        assert sent == [{"addLicenses": [{"skuId": COPILOT_SKU}], "removeLicenses": []}]
        assert licences.state["AGENT_USER_WORK_IQ_LICENSE_SKU"] == "Microsoft_365_Copilot"
        assert "AGENT_USER_LICENSE_SKU" not in licences.state

    def test_a_licence_the_agent_already_has_is_recorded_and_not_given_again(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._assignable(licences, BUSINESS_PREMIUM, agent=[TEAMS_SKU])

        assert licences.module.main(["--assign-licence", TEAMS_SKU]) == 0

        assert _result_lines(capsys.readouterr().out) == [
            LICENCE_MARKERS["LICENCE_ALREADY_ASSIGNED_MARKER"]
        ]
        assert licences.graph.writes == []
        assert licences.state["AGENT_USER_LICENSE_SKU"] == "SPB"

    def test_no_free_seat_is_said_before_anything_changes(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        full = dict(BUSINESS_PREMIUM, consumedUnits=2, prepaidUnits={"enabled": 2})
        self._assignable(licences, full)

        assert licences.module.main(["--assign-licence", TEAMS_SKU]) == 1

        assert _the_one_marker(capsys.readouterr().out) == (
            LICENCE_MARKERS["LICENCE_NO_FREE_SEAT_MARKER"]
        )
        assert licences.graph.writes == []

    @pytest.mark.parametrize(
        "asked",
        [DEVICE_SKU, "44444444-aaaa-4000-8000-000000000004", "not-an-id"],
        ids=["shared-devices", "not-in-the-tenant", "not-an-id"],
    )
    def test_a_licence_that_is_not_one_for_the_agent_is_never_given(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str], asked: str
    ) -> None:
        self._assignable(licences, BUSINESS_PREMIUM, SHARED_DEVICES)

        assert licences.module.main(["--assign-licence", asked]) == 1

        assert _the_one_marker(capsys.readouterr().out) == (
            LICENCE_MARKERS["LICENCE_NOT_FOUND_MARKER"]
        )
        assert licences.graph.writes == []

    def test_a_refused_assignment_says_so(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._assignable(licences, BUSINESS_PREMIUM)
        licences.answer(
            f"/users/{AGENT_USER_OBJECT}/assignLicense",
            _resp(400, {"error": {"code": "Request_BadRequest"}}),
            method="POST",
        )

        assert licences.module.main(["--assign-licence", TEAMS_SKU]) == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == LICENCE_MARKERS["LICENCE_ASSIGN_FAILED_MARKER"]
        assert "AGENT_USER_LICENSE_SKU" not in licences.state

    def test_a_usage_location_that_cannot_be_set_stops_before_the_assignment(
        self, licences: Lookup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._assignable(licences, BUSINESS_PREMIUM)
        licences.answer(f"/users/{AGENT_USER_OBJECT}", _resp(404, {}), method="PATCH")

        assert licences.module.main(["--assign-licence", TEAMS_SKU]) == 1

        out = capsys.readouterr().out
        assert _the_one_marker(out) == LICENCE_MARKERS["LICENCE_ASSIGN_FAILED_MARKER"]
        assert ("POST", f"/users/{AGENT_USER_OBJECT}/assignLicense") not in licences.graph.calls

    def test_with_no_arguments_it_is_the_full_run_as_before(
        self, agent_ids_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[str] = []
        monkeypatch.setattr(agent_ids_module, "get_existing_graph_token", lambda: "tok")
        monkeypatch.setattr(agent_ids_module, "_provision", lambda token: ran.append(token))
        assert agent_ids_module.main() == 0
        assert ran == ["tok"]
