"""Tests for scripts/show_permissions.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "show_permissions.py"
)
spec = importlib.util.spec_from_file_location("show_permissions", _SCRIPT)
perms_mod = importlib.util.module_from_spec(spec)
sys.modules["show_permissions"] = perms_mod

# Stub entra_provisioning
_prov_stub = MagicMock()
_prov_stub.get_existing_graph_token.return_value = "tok-test"
_prov_stub.get_state.return_value = None
_prov_stub.ProvisionerBootstrapError = type(
    "ProvisionerBootstrapError", (Exception,), {}
)
sys.modules.setdefault("entra_provisioning", _prov_stub)


def _load_module():
    spec.loader.exec_module(perms_mod)


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else json.dumps(body)
    r.json.return_value = body or {}
    return r


# ---------------------------------------------------------------------------
AGENT_OID = "agent-obj-111"
AGENT_USER_ID = "user-222"

GRANTS = [
    {
        "id": "g1",
        "clientId": AGENT_OID,
        "principalId": AGENT_USER_ID,
        "resourceId": "res-graph",
        "scope": "Chat.ReadWrite Mail.Send User.Read",
        "consentType": "Principal",
    },
    {
        "id": "g2",
        "clientId": AGENT_OID,
        "principalId": AGENT_USER_ID,
        "resourceId": "res-storage",
        "scope": "user_impersonation",
        "consentType": "Principal",
    },
]

SP_NAMES = {
    "res-graph": "Microsoft Graph",
    "res-storage": "Azure Storage",
}


class TestShowPermissions:
    """Show permissions for the Agent Identity."""

    @patch.dict("os.environ", {}, clear=False)
    def test_show(self, capsys):
        _load_module()

        def fake_graph(method, path, token, **kw):
            if "oauth2PermissionGrants" in path:
                return _json_resp(200, {"value": GRANTS})
            for sp_id, name in SP_NAMES.items():
                if f"/servicePrincipals/{sp_id}" in path:
                    return _json_resp(200, {"displayName": name})
            return _json_resp(404)

        with (
            patch.object(perms_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(
                perms_mod,
                "get_state",
                side_effect=lambda k: {
                    "AGENT_OBJECT_ID": AGENT_OID,
                    "AGENT_USER_ID": AGENT_USER_ID,
                }.get(k),
            ),
            patch.object(perms_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = perms_mod.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Chat.ReadWrite" in out
        assert "Mail.Send" in out
        assert "user_impersonation" in out
        assert "Microsoft Graph" in out
        assert "Azure Storage" in out


class TestJsonOutput:
    """--json produces valid JSON."""

    @patch.dict("os.environ", {}, clear=False)
    def test_json(self, capsys):
        _load_module()

        def fake_graph(method, path, token, **kw):
            if "oauth2PermissionGrants" in path:
                return _json_resp(200, {"value": GRANTS})
            for sp_id, name in SP_NAMES.items():
                if f"/servicePrincipals/{sp_id}" in path:
                    return _json_resp(200, {"displayName": name})
            return _json_resp(404)

        with (
            patch.object(perms_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(
                perms_mod,
                "get_state",
                side_effect=lambda k: {
                    "AGENT_OBJECT_ID": AGENT_OID,
                    "AGENT_USER_ID": AGENT_USER_ID,
                }.get(k),
            ),
            patch.object(perms_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = perms_mod.main(["--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 2
        assert "Chat.ReadWrite" in data[0]["scopes"]
        assert data[0]["resourceName"] == "Microsoft Graph"
        assert data[1]["resourceName"] == "Azure Storage"


class TestNoGrants:
    """No grants found."""

    @patch.dict("os.environ", {}, clear=False)
    def test_empty(self, capsys):
        _load_module()

        def fake_graph(method, path, token, **kw):
            if "oauth2PermissionGrants" in path:
                return _json_resp(200, {"value": []})
            return _json_resp(404)

        with (
            patch.object(perms_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(
                perms_mod,
                "get_state",
                side_effect=lambda k: {
                    "AGENT_OBJECT_ID": AGENT_OID,
                    "AGENT_USER_ID": AGENT_USER_ID,
                }.get(k),
            ),
            patch.object(perms_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = perms_mod.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No permission" in out or "no grant" in out.lower()


class TestMissingState:
    """Error when required state is missing."""

    @patch.dict("os.environ", {}, clear=False)
    def test_missing(self):
        _load_module()
        with (
            patch.object(perms_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(perms_mod, "get_state", return_value=None),
        ):
            rc = perms_mod.main([])
        assert rc == 1


class TestCLIHelp:
    """CLI --help exits 0."""

    @patch.dict("os.environ", {}, clear=False)
    def test_help(self):
        _load_module()
        with pytest.raises(SystemExit) as exc:
            perms_mod.main(["--help"])
        assert exc.value.code == 0
