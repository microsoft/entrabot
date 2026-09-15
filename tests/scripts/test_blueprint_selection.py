"""An explicit Blueprint must override cached state without falling back to another."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def identities(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "create_entra_agent_ids.py"
    spec = importlib.util.spec_from_file_location("selected_blueprint_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_FORCE_NEW", False)
    monkeypatch.setenv("_ENTRABOT_USE_BLUEPRINT", "selected-app")
    monkeypatch.setattr(module, "get_state", Mock(side_effect=AssertionError("stale state read")))
    monkeypatch.setattr(module, "set_state", Mock())
    monkeypatch.setattr(module, "ensure_blueprint_principal", Mock())
    return module


def test_explicit_blueprint_wins_over_cached_identity(identities, monkeypatch):
    request = Mock(return_value=Mock(status_code=200, json=lambda: {
        "value": [{"id": "selected-object", "appId": "selected-app"}],
    }))
    monkeypatch.setattr(identities, "graph_request", request)

    assert identities.create_blueprint("fixture-token") == ("selected-app", "selected-object")
    assert request.call_args.args[:2] == (
        "GET", "/applications?$filter=appId eq 'selected-app'",
    )
    identities.set_state.assert_any_call("BLUEPRINT_OBJECT_ID", "selected-object")


@pytest.mark.parametrize("status,values", [
    (200, []),
    (403, []),
    (200, [{"id": "wrong-object", "appId": "wrong-app"}]),
])
def test_invalid_selected_blueprint_never_creates_or_uses_another(
    identities, monkeypatch, status, values,
):
    request = Mock(return_value=Mock(status_code=status, json=lambda: {"value": values}))
    monkeypatch.setattr(identities, "graph_request", request)

    with pytest.raises(identities.ProvisionerBootstrapError, match="selected Blueprint"):
        identities.create_blueprint("fixture-token")

    assert request.call_count == 1
    identities.set_state.assert_not_called()
    identities.ensure_blueprint_principal.assert_not_called()


def test_existing_upn_under_another_blueprint_is_not_adopted(identities, monkeypatch):
    monkeypatch.setattr(identities, "get_existing_graph_token", lambda: "fixture-token")
    monkeypatch.setattr(identities, "create_blueprint", lambda token: ("selected-app", "object"))
    monkeypatch.setattr(identities, "_agent_user_upn", lambda token: "other@example.test")
    monkeypatch.setattr(identities, "find_existing_agent_user_by_upn", lambda *a: {
        "id": "other-user", "userPrincipalName": "other@example.test",
        "identityParentId": "other-agent",
    })
    monkeypatch.setattr(identities, "_servicePrincipal_by_object_id", lambda *a: {
        "appId": "other-app", "agentIdentityBlueprintId": "different-blueprint",
    })
    grant = Mock(side_effect=AssertionError("must not grant to another chain"))
    monkeypatch.setattr(identities, "grant_agent_identity_app_permissions", grant)

    assert identities.main() == 1
    grant.assert_not_called()
    identities.set_state.assert_not_called()
