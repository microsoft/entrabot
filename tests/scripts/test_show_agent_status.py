"""Tests for scripts/show_agent_status.py."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "show_agent_status.py"
spec = importlib.util.spec_from_file_location("show_agent_status", _SCRIPT)
status_mod = importlib.util.module_from_spec(spec)
sys.modules["show_agent_status"] = status_mod

# Stub entra_provisioning before the module is exec'd
_prov_stub = MagicMock()
_prov_stub.get_existing_graph_token.return_value = "tok-test"
_prov_stub.get_state.return_value = None
_prov_stub.ProvisionerBootstrapError = type("ProvisionerBootstrapError", (Exception,), {})
sys.modules.setdefault("entra_provisioning", _prov_stub)


def _load_module():
    spec.loader.exec_module(status_mod)


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else json.dumps(body)
    r.json.return_value = body or {}
    return r


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
STATE = {
    "TENANT_ID": "tenant-111",
    "BLUEPRINT_APP_ID": "bp-app-222",
    "BLUEPRINT_OBJECT_ID": "bp-obj-333",
    "AGENT_ID": "agent-app-444",
    "AGENT_OBJECT_ID": "agent-app-444",
    "AGENT_USER_ID": "user-666",
    "AGENT_USER_UPN": "agent@example.com",
    "PROVISIONER_CLIENT_ID": "prov-777",
    "PROVISIONER_CERT_THUMBPRINT": "AABB",
    "AGENT_USER_WORK_IQ_LICENSE_SKU": "Microsoft_365_Copilot",
}

_FAKE_CERT_DER = b"fake-cert-der-bytes-for-status-test"
_FAKE_CERT_B64 = base64.b64encode(_FAKE_CERT_DER).decode()
_FAKE_CERT_THUMB = (
    base64.urlsafe_b64encode(hashlib.sha256(_FAKE_CERT_DER).digest()).rstrip(b"=").decode()
)
STATE["BLUEPRINT_CERT_THUMBPRINT"] = _FAKE_CERT_THUMB

SPONSORS = [
    {
        "id": "s1",
        "displayName": "Alice",
        "userPrincipalName": "alice@example.com",
        "mail": "alice@example.com",
    },
]

LICENSES = {
    "value": [{"skuId": "sku-1", "skuPartNumber": "ENTERPRISEPACK"}],
}

GRANTS = {
    "value": [
        {
            "id": "g1",
            "resourceId": "res-1",
            "consentType": "Principal",
            "scope": "Chat.ReadWrite Mail.Send",
        },
    ],
}


def _fake_graph(method, path, token, **kw):
    if "/sponsors" in path:
        return _json_resp(200, {"value": SPONSORS})
    if "assignedLicenses" in path:
        return _json_resp(200, {"assignedLicenses": [{"skuId": "sku-1"}]})
    if "subscribedSkus" in path:
        return _json_resp(200, LICENSES)
    if "oauth2PermissionGrants" in path:
        return _json_resp(200, GRANTS)
    if "/applications/" in path and "keyCredentials" in path:
        return _json_resp(
            200,
            {
                "keyCredentials": [
                    {
                        "keyId": "key-1",
                        "displayName": "EntraClaw Device Certificate - TestHost",
                        "type": "AsymmetricX509Cert",
                        "usage": "Verify",
                        "key": _FAKE_CERT_B64,
                        "customKeyIdentifier": "sha1-ish",
                        "endDateTime": "2027-01-01T00:00:00Z",
                    }
                ]
            },
        )
    if "/servicePrincipals/res-1" in path:
        return _json_resp(200, {"displayName": "Microsoft Graph"})
    return _json_resp(200, {})


class TestShowStatus:
    """Happy path: show full status."""

    @patch.dict(
        "os.environ",
        {
            "ENTRACLAW_BLOB_ENDPOINT": "https://entclawtest.blob.core.windows.net",
            "ENTRACLAW_BLOB_CONTAINER": "agent-user-666",
            "ENTRACLAW_RESOURCE_GROUP": "rg-test",
        },
        clear=False,
    )
    def test_full_status(self, capsys):
        _load_module()

        def fake_state(key):
            return STATE.get(key)

        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=fake_state),
            patch.object(status_mod, "graph_request", side_effect=_fake_graph),
        ):
            rc = status_mod.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "bp-app-222" in out
        assert "agent-app-444" in out
        assert "agent@example.com" in out
        assert "Alice" in out
        assert "ENTERPRISEPACK" in out
        assert "Health" in out or "pass" in out.lower()
        assert "Local Platform" in out
        assert "Storage" in out
        assert "rg-test" in out
        assert "entclawtest" in out
        assert "Microsoft Graph" in out
        assert "Blueprint Key Credentials" in out
        assert "key-1" in out
        assert "EntraClaw Device Certificate - TestHost" in out
        assert "Matches Local Cert:   yes" in out


class TestShowStatusJson:
    """--json produces valid JSON output."""

    @patch.dict("os.environ", {}, clear=False)
    def test_json_output(self, capsys):
        _load_module()

        def fake_state(key):
            return STATE.get(key)

        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=fake_state),
            patch.object(status_mod, "graph_request", side_effect=_fake_graph),
        ):
            rc = status_mod.main(["--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["blueprint"]["app_object_id"] == "bp-app-222"
        assert data["agent_identity"]["app_object_id"] == "agent-app-444"
        assert data["health"]["failed"] == 0
        assert data["licenses"][0]["skuPartNumber"] == "ENTERPRISEPACK"
        assert data["permissions"][0]["resourceName"] == "Microsoft Graph"
        assert (
            data["key_credentials"][0]["displayName"] == "EntraClaw Device Certificate - TestHost"
        )
        assert data["key_credentials"][0]["matchesLocalBlueprintCert"] is True


class TestMissingState:
    """Graceful handling when state is incomplete."""

    @patch.dict("os.environ", {}, clear=False)
    def test_partial_state(self, capsys):
        _load_module()

        def fake_state(key):
            # Only tenant is set
            if key == "TENANT_ID":
                return "tenant-111"
            return None

        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=fake_state),
        ):
            rc = status_mod.main([])
        # Should still succeed — shows what's available
        assert rc == 0
        out = capsys.readouterr().out
        assert "tenant-111" in out
        assert "not set" in out.lower() or "N/A" in out


class TestTokenFailure:
    """Error when provisioner token cannot be acquired."""

    @patch.dict("os.environ", {}, clear=False)
    def test_token_fail(self):
        _load_module()
        exc_type = status_mod.ProvisionerBootstrapError
        with patch.object(status_mod, "get_existing_graph_token", side_effect=exc_type("no cert")):
            rc = status_mod.main([])
        assert rc == 1


class TestCLIHelp:
    """CLI --help exits 0."""

    @patch.dict("os.environ", {}, clear=False)
    def test_help(self):
        _load_module()
        with pytest.raises(SystemExit) as exc:
            status_mod.main(["--help"])
        assert exc.value.code == 0
