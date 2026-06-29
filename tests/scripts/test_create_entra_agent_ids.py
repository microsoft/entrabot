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
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "create_entra_agent_ids.py"


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


class TestCreateBlueprint:
    def test_reuses_pinned_blueprint_when_force_new_chain_targets_existing_blueprint(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved: dict[str, str] = {}
        ensured: list[str] = []
        calls: list[tuple[str, str]] = []

        def fake_graph_request(method, path, token, **kw):
            del token, kw
            calls.append((method, path))
            if path == f"/applications?$filter=appId eq '{BLUEPRINT_OURS}'":
                return _resp(
                    200,
                    {"value": [{"id": "blueprint-obj", "appId": BLUEPRINT_OURS}]},
                )
            raise AssertionError(f"unexpected Graph call: {method} {path}")

        monkeypatch.setattr(agent_ids_module, "_FORCE_NEW", True)
        monkeypatch.setattr(agent_ids_module, "_REUSE_BLUEPRINT", True)
        monkeypatch.setattr(agent_ids_module, "_PINNED_BLUEPRINT_APP_ID", BLUEPRINT_OURS)
        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(agent_ids_module, "set_state", saved.__setitem__)
        monkeypatch.setattr(
            agent_ids_module,
            "ensure_blueprint_principal",
            lambda token, app_id: ensured.append(app_id),
        )

        result = agent_ids_module.create_blueprint("tok")

        assert result == (BLUEPRINT_OURS, "blueprint-obj")
        assert saved == {
            "BLUEPRINT_APP_ID": BLUEPRINT_OURS,
            "BLUEPRINT_OBJECT_ID": "blueprint-obj",
        }
        assert ensured == [BLUEPRINT_OURS]
        assert not any(path == "/applications" for _, path in calls)

    def test_pinned_blueprint_missing_fails_without_display_name_fallback(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str]] = []

        def fake_graph_request(method, path, token, **kw):
            del token, kw
            calls.append((method, path))
            if path == f"/applications?$filter=appId eq '{BLUEPRINT_OURS}'":
                return _resp(200, {"value": []})
            raise AssertionError(f"unexpected Graph call: {method} {path}")

        monkeypatch.setattr(agent_ids_module, "_PINNED_BLUEPRINT_APP_ID", BLUEPRINT_OURS)
        monkeypatch.setattr(agent_ids_module, "_FORCE_NEW", False)
        monkeypatch.setattr(agent_ids_module, "_REUSE_BLUEPRINT", False)
        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        with pytest.raises(SystemExit):
            agent_ids_module.create_blueprint("tok")

        assert calls == [("GET", f"/applications?$filter=appId eq '{BLUEPRINT_OURS}'")]


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
