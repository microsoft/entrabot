"""Tests for scripts/revoke_consent.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the script as a module
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "revoke_consent.py"
spec = importlib.util.spec_from_file_location("revoke_consent", _SCRIPT)
revoke_mod = importlib.util.module_from_spec(spec)
sys.modules["revoke_consent"] = revoke_mod


def _load_module():
    spec.loader.exec_module(revoke_mod)


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else str(body)
    r.json.return_value = body or {}
    return r


AGENT_OBJ_ID = "agent-obj-111"
AGENT_USER_ID = "agent-user-222"
GRANT_ID = "grant-333"

FULL_GRANT = {
    "id": GRANT_ID,
    "clientId": AGENT_OBJ_ID,
    "principalId": AGENT_USER_ID,
    "resourceId": "graph-sp-1",  # Microsoft Graph, the default resource
    "scope": "Chat.Create Chat.ReadWrite Mail.Read Files.ReadWrite",
}


@pytest.fixture(autouse=True)
def _patch_provisioning():
    prov = MagicMock()
    prov.get_graph_token.return_value = "fake-token"
    prov.get_state.side_effect = lambda k: {
        "AGENT_OBJECT_ID": AGENT_OBJ_ID,
        "AGENT_USER_ID": AGENT_USER_ID,
    }.get(k)
    prov.ProvisionerBootstrapError = type("ProvisionerBootstrapError", (Exception,), {})
    sys.modules["entra_provisioning"] = prov
    _load_module()
    yield prov
    sys.modules.pop("entra_provisioning", None)


class TestRevokeSpecificScopes:
    """Revoke a subset of scopes from an existing grant."""

    @patch.object(revoke_mod, "requests")
    def test_patch_removes_scopes(self, mock_requests, capsys):
        """Removing some scopes -> PATCH with the remainder."""
        mock_requests.get.side_effect = _router([FULL_GRANT])
        mock_requests.patch.return_value = _json_resp(204)

        rc = revoke_mod.main(["prog", "--scopes", "Mail.Read,Files.ReadWrite"])
        assert rc == 0

        # PATCH was called
        patch_call = mock_requests.patch.call_args
        patched_scopes = set(patch_call[1]["json"]["scope"].split())
        assert "Mail.Read" not in patched_scopes
        assert "Files.ReadWrite" not in patched_scopes
        assert "Chat.Create" in patched_scopes
        assert "Chat.ReadWrite" in patched_scopes

    @patch.object(revoke_mod, "requests")
    def test_remove_all_scopes_deletes_grant(self, mock_requests, capsys):
        """Removing ALL scopes -> DELETE the entire grant."""
        grant = {**FULL_GRANT, "scope": "Mail.Read"}
        mock_requests.get.side_effect = _router([grant])
        mock_requests.delete.return_value = _json_resp(204)

        rc = revoke_mod.main(["prog", "--scopes", "Mail.Read"])
        assert rc == 0
        mock_requests.delete.assert_called_once()

    @patch.object(revoke_mod, "requests")
    def test_scope_not_in_grant(self, mock_requests, capsys):
        """Scope to remove isn't in the grant -> no-op."""
        mock_requests.get.side_effect = _router([FULL_GRANT])

        rc = revoke_mod.main(["prog", "--scopes", "NotReal.Scope"])
        assert rc == 0
        mock_requests.patch.assert_not_called()
        mock_requests.delete.assert_not_called()
        out = capsys.readouterr().out
        assert "not present" in out.lower() or "no matching" in out.lower()


class TestNoGrant:
    @patch.object(revoke_mod, "requests")
    def test_no_existing_grant(self, mock_requests, capsys):
        """No consent grant found -> error."""
        mock_requests.get.side_effect = _router([])

        rc = revoke_mod.main(["prog", "--scopes", "Mail.Read"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "no consent grant" in out.lower() or "not found" in out.lower()


class TestCLIArgs:
    def test_help(self, capsys):
        rc = revoke_mod.main(["prog", "--help"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "usage: revoke_consent.py" in out

    def test_no_scopes_arg(self, capsys):
        rc = revoke_mod.main(["prog"])
        assert rc == 2

    @patch.object(revoke_mod, "requests")
    def test_missing_state(self, mock_requests, _patch_provisioning, capsys):
        _patch_provisioning.get_state.side_effect = lambda k: None
        rc = revoke_mod.main(["prog", "--scopes", "Mail.Read"])
        assert rc == 1
        mock_requests.get.assert_not_called()


class TestRevokeAll:
    """--all flag removes every scope."""

    @patch.object(revoke_mod, "requests")
    def test_all_flag_deletes_grant(self, mock_requests, capsys):
        mock_requests.get.side_effect = _router([FULL_GRANT])
        mock_requests.delete.return_value = _json_resp(204)

        rc = revoke_mod.main(["prog", "--all"])
        assert rc == 0
        mock_requests.delete.assert_called_once()


# ---------------------------------------------------------------------------
# One grant per resource: the Agent Identity holds a grant on Microsoft Graph,
# one on Azure Storage, and can hold one on any other API. The script must
# change only the grant on the resource it was asked about.
# ---------------------------------------------------------------------------

GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
STORAGE_APP_ID = "e406a681-f3d4-42a8-90b6-c2b029497af1"
OTHER_API_APP_ID = "11111111-2222-3333-4444-555555555555"

SP_IDS = {
    GRAPH_APP_ID: "graph-sp-1",
    STORAGE_APP_ID: "storage-sp-2",
    OTHER_API_APP_ID: "other-api-sp-3",
}

STORAGE_GRANT = {
    "id": "grant-storage",
    "clientId": AGENT_OBJ_ID,
    "principalId": AGENT_USER_ID,
    "resourceId": "storage-sp-2",
    "scope": "user_impersonation",
}
GRAPH_GRANT = {**FULL_GRANT, "id": "grant-graph", "resourceId": "graph-sp-1"}
OTHER_API_GRANT = {
    "id": "grant-other-api",
    "clientId": AGENT_OBJ_ID,
    "principalId": AGENT_USER_ID,
    "resourceId": "other-api-sp-3",
    "scope": "Ledger.Read",
}


def _router(grants: list[dict]):
    """Answer the service-principal lookup and the grant listing by URL."""

    def _get(url, *args, **kwargs):
        if "/servicePrincipals" in url:
            for app_id, sp_id in SP_IDS.items():
                if app_id in url:
                    return _json_resp(200, {"value": [{"id": sp_id, "appId": app_id}]})
            return _json_resp(200, {"value": []})
        return _json_resp(200, {"value": list(grants)})

    return _get


class TestOneResource:
    @patch.object(revoke_mod, "requests")
    def test_default_is_the_graph_grant_even_when_storage_is_listed_first(
        self, mock_requests, capsys
    ):
        mock_requests.get.side_effect = _router([STORAGE_GRANT, GRAPH_GRANT])
        mock_requests.patch.return_value = _json_resp(204)

        rc = revoke_mod.main(["prog", "--scopes", "Mail.Read"])

        assert rc == 0
        patched_url = mock_requests.patch.call_args[0][0]
        assert patched_url.endswith("/oauth2PermissionGrants/grant-graph")
        mock_requests.delete.assert_not_called()

    @patch.object(revoke_mod, "requests")
    def test_all_with_resource_app_id_deletes_only_that_grant(self, mock_requests, capsys):
        mock_requests.get.side_effect = _router([GRAPH_GRANT, STORAGE_GRANT, OTHER_API_GRANT])
        mock_requests.delete.return_value = _json_resp(204)

        rc = revoke_mod.main(["prog", "--all", "--resource-app-id", OTHER_API_APP_ID])

        assert rc == 0
        mock_requests.delete.assert_called_once()
        deleted_url = mock_requests.delete.call_args[0][0]
        assert deleted_url.endswith("/oauth2PermissionGrants/grant-other-api")
        mock_requests.patch.assert_not_called()

    @patch.object(revoke_mod, "requests")
    def test_no_grant_on_that_resource_changes_nothing(self, mock_requests, capsys):
        mock_requests.get.side_effect = _router([GRAPH_GRANT, STORAGE_GRANT])

        rc = revoke_mod.main(["prog", "--all", "--resource-app-id", OTHER_API_APP_ID])

        assert rc == 1
        mock_requests.delete.assert_not_called()
        mock_requests.patch.assert_not_called()
        assert "no consent grant" in capsys.readouterr().out.lower()

    @patch.object(revoke_mod, "requests")
    def test_unknown_resource_app_id_changes_nothing(self, mock_requests, capsys):
        mock_requests.get.side_effect = _router([GRAPH_GRANT])

        rc = revoke_mod.main(
            ["prog", "--all", "--resource-app-id", "99999999-0000-0000-0000-000000000000"]
        )

        assert rc == 1
        mock_requests.delete.assert_not_called()
        mock_requests.patch.assert_not_called()

    def test_help_names_the_resource_option(self, capsys):
        assert revoke_mod.main(["prog", "--help"]) == 0
        assert "--resource-app-id" in capsys.readouterr().out
