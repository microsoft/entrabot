"""Tests for targeted Agent User teardown.

The live cleanup path must release paid licenses before deleting the Agent
User, then delete the Agent Identity and Blueprint without touching Azure Blob
Storage. These tests keep that sequence explicit and unit-testable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import entrabot.graph_helpers as _graph_helpers_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "deprovision_entra_agent_identity.py"


@pytest.fixture
def deprovision_module():
    spec = importlib.util.spec_from_file_location(
        "deprovision_entra_agent_identity", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["deprovision_entra_agent_identity"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("deprovision_entra_agent_identity", None)


def _resp(status: int, body: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        json=lambda: body or {},
        text=str(body or {}),
    )


def _fake_resolver(method: str, path: str, token: str, **kw) -> SimpleNamespace:
    del token, kw
    if method == "GET" and path.startswith("/users?$filter=userPrincipalName eq"):
        return _resp(
            200,
            {
                "value": [
                    {
                        "id": "agent-user-id",
                        "userPrincipalName": "entrabot-agent-sati-agent@fabrikam.onmicrosoft.com",
                        "identityParentId": "agent-identity-object-id",
                        "assignedLicenses": [
                            {"skuId": "teams-sku"},
                            {"skuId": "copilot-sku"},
                        ],
                    }
                ]
            },
        )
    if method == "GET" and path == "/servicePrincipals/agent-identity-object-id":
        return _resp(
            200,
            {
                "id": "agent-identity-object-id",
                "appId": "agent-identity-app-id",
                "displayName": "EntraBot Agent (sati-agent)",
                "agentIdentityBlueprintId": "blueprint-app-id",
            },
        )
    if method == "GET" and path.startswith("/applications?$filter=appId eq"):
        return _resp(
            200,
            {
                "value": [
                    {
                        "id": "blueprint-object-id",
                        "appId": "blueprint-app-id",
                        "displayName": "EntraBot Code Agent",
                    }
                ]
            },
        )
    if method == "GET" and path.startswith(
        "/servicePrincipals/microsoft.graph.agentIdentity"
    ):
        return _resp(
            200,
            {
                "value": [
                    {
                        "id": "agent-identity-object-id",
                        "appId": "agent-identity-app-id",
                    }
                ]
            },
        )
    raise AssertionError(f"unexpected Graph call: {method} {path}")


def test_deprovisions_license_user_agent_identity_and_blueprint_in_order(
    deprovision_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_graph_request(method, path, token, **kw):
        calls.append((method, path, kw.get("json_body")))
        if method == "GET":
            return _fake_resolver(method, path, token, **kw)
        return _resp(204 if method == "DELETE" else 200, {})

    monkeypatch.setattr(deprovision_module, "graph_request", fake_graph_request)
    monkeypatch.setattr(_graph_helpers_mod, "graph_request", fake_graph_request)

    result = deprovision_module.deprovision_agent_user(
        "token", "entrabot-agent-sati-agent@fabrikam.onmicrosoft.com"
    )

    assert result == "deleted"
    assert calls == [
        (
            "GET",
            "/users?$filter=userPrincipalName eq "
            "'entrabot-agent-sati-agent@fabrikam.onmicrosoft.com'"
            "&$select=id,userPrincipalName,identityParentId,assignedLicenses,"
            "licenseAssignmentStates",
            None,
        ),
        ("GET", "/servicePrincipals/agent-identity-object-id", None),
        (
            "GET",
            "/applications?$filter=appId eq 'blueprint-app-id'"
            "&$select=id,appId,displayName",
            None,
        ),
        (
            "GET",
            "/servicePrincipals/microsoft.graph.agentIdentity"
            "?$select=id,appId,displayName,agentIdentityBlueprintId&$top=999",
            None,
        ),
        (
            "POST",
            "/users/agent-user-id/assignLicense",
            {"addLicenses": [], "removeLicenses": ["teams-sku", "copilot-sku"]},
        ),
        ("DELETE", "/users/agent-user-id", None),
        ("DELETE", "/servicePrincipals/agent-identity-object-id", None),
        ("DELETE", "/applications/blueprint-object-id", None),
    ]


def test_license_removal_failure_stops_before_deleting_user(
    deprovision_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_graph_request(method, path, token, **kw):
        calls.append((method, path))
        if method == "GET":
            return _fake_resolver(method, path, token, **kw)
        if path.endswith("/assignLicense"):
            return _resp(400, {"error": "license failed"})
        return _resp(204, {})

    monkeypatch.setattr(deprovision_module, "graph_request", fake_graph_request)
    monkeypatch.setattr(_graph_helpers_mod, "graph_request", fake_graph_request)

    with pytest.raises(RuntimeError, match="Failed to remove licenses"):
        deprovision_module.deprovision_agent_user(
            "token", "entrabot-agent-sati-agent@fabrikam.onmicrosoft.com"
        )

    assert ("DELETE", "/users/agent-user-id") not in calls
    assert ("DELETE", "/servicePrincipals/agent-identity-object-id") not in calls
    assert ("DELETE", "/applications/blueprint-object-id") not in calls


def test_only_direct_licenses_are_removed_when_group_licenses_exist(
    deprovision_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_graph_request(method, path, token, **kw):
        calls.append((method, path, kw.get("json_body")))
        if method == "GET" and path.startswith("/users?$filter=userPrincipalName eq"):
            return _resp(
                200,
                {
                    "value": [
                        {
                            "id": "agent-user-id",
                            "userPrincipalName": (
                                "entrabot-agent-sati-agent"
                                "@fabrikam.onmicrosoft.com"
                            ),
                            "identityParentId": "agent-identity-object-id",
                            "assignedLicenses": [
                                {"skuId": "direct-teams-sku"},
                                {"skuId": "group-copilot-sku"},
                            ],
                            "licenseAssignmentStates": [
                                {"skuId": "direct-teams-sku", "assignedByGroup": None},
                                {
                                    "skuId": "group-copilot-sku",
                                    "assignedByGroup": "license-group-id",
                                },
                            ],
                        }
                    ]
                },
            )
        if method == "GET":
            return _fake_resolver(method, path, token, **kw)
        return _resp(204 if method == "DELETE" else 200, {})

    monkeypatch.setattr(deprovision_module, "graph_request", fake_graph_request)
    monkeypatch.setattr(_graph_helpers_mod, "graph_request", fake_graph_request)

    result = deprovision_module.deprovision_agent_user(
        "token", "entrabot-agent-sati-agent@fabrikam.onmicrosoft.com"
    )

    assert result == "deleted"
    assign_calls = [call for call in calls if call[1].endswith("/assignLicense")]
    assert assign_calls == [
        (
            "POST",
            "/users/agent-user-id/assignLicense",
            {"addLicenses": [], "removeLicenses": ["direct-teams-sku"]},
        )
    ]


def test_dry_run_resolves_chain_but_does_not_mutate_graph(
    deprovision_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_graph_request(method, path, token, **kw):
        calls.append((method, path))
        return _fake_resolver(method, path, token, **kw)

    monkeypatch.setattr(deprovision_module, "graph_request", fake_graph_request)
    monkeypatch.setattr(_graph_helpers_mod, "graph_request", fake_graph_request)

    result = deprovision_module.deprovision_agent_user(
        "token", "entrabot-agent-sati-agent@fabrikam.onmicrosoft.com", dry_run=True
    )

    assert result == "dry-run"
    assert all(method == "GET" for method, _ in calls)


def test_refuses_to_delete_blueprint_with_other_agent_identities(
    deprovision_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_graph_request(method, path, token, **kw):
        if method == "GET" and path.startswith(
            "/servicePrincipals/microsoft.graph.agentIdentity"
        ):
            return _resp(
                200,
                {
                    "value": [
                        {
                            "id": "agent-identity-object-id",
                            "agentIdentityBlueprintId": "blueprint-app-id",
                        },
                        {
                            "id": "other-agent-identity-object-id",
                            "agentIdentityBlueprintId": "blueprint-app-id",
                        },
                    ]
                },
            )
        return _fake_resolver(method, path, token, **kw)

    monkeypatch.setattr(deprovision_module, "graph_request", fake_graph_request)
    monkeypatch.setattr(_graph_helpers_mod, "graph_request", fake_graph_request)

    with pytest.raises(RuntimeError, match="Blueprint has 1 other Agent Identity"):
        deprovision_module.deprovision_agent_user(
            "token", "entrabot-agent-sati-agent@fabrikam.onmicrosoft.com"
        )


def test_missing_agent_user_is_idempotent(
    deprovision_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        deprovision_module,
        "graph_request",
        lambda *args, **kwargs: _resp(200, {"value": []}),
    )

    result = deprovision_module.deprovision_agent_user("token", "missing@contoso.com")

    assert result == "missing"
