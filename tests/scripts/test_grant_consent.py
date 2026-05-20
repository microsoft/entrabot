"""Tests for scripts/grant_consent.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading boilerplate
# ---------------------------------------------------------------------------
_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "grant_consent.py"
)
spec = importlib.util.spec_from_file_location("grant_consent", _SCRIPT)
consent_mod = importlib.util.module_from_spec(spec)
sys.modules["grant_consent"] = consent_mod

# Stub entra_provisioning before the module is exec'd
_prov_stub = MagicMock()
_prov_stub.get_existing_graph_token.return_value = "tok-test"
_prov_stub.get_state.return_value = None
_prov_stub.set_state = MagicMock()
_prov_stub.ProvisionerBootstrapError = type(
    "ProvisionerBootstrapError", (Exception,), {}
)
sys.modules.setdefault("entra_provisioning", _prov_stub)


def _load_module():
    spec.loader.exec_module(consent_mod)


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else str(body)
    r.json.return_value = body or {}
    return r


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AGENT_OID = "agent-oid-aaa"
AGENT_USER_ID = "user-oid-bbb"
GRAPH_SP_OID = "graph-sp-ccc"
GRANT_ID = "grant-id-ddd"
MS_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestGrantNewConsent:
    """POST a new oauth2PermissionGrant when none exists."""

    @patch.dict("os.environ", {}, clear=False)
    def test_new_grant(self):
        _load_module()

        def fake_requests_method(method_name):
            def handler(url, **kwargs):
                if "servicePrincipals" in url and "appId" in url:
                    return _json_resp(
                        200, {"value": [{"id": GRAPH_SP_OID}]}
                    )
                if "oauth2PermissionGrants" in url and method_name == "get":
                    return _json_resp(200, {"value": []})
                if "oauth2PermissionGrants" in url and method_name == "post":
                    return _json_resp(201, {})
                return _json_resp(404)
            return handler

        mock_requests = MagicMock()
        mock_requests.get.side_effect = fake_requests_method("get")
        mock_requests.post.side_effect = fake_requests_method("post")

        with (
            patch.object(consent_mod, "requests", mock_requests),
            patch.object(
                consent_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(
                consent_mod,
                "get_state",
                side_effect=lambda k: {
                    "AGENT_OBJECT_ID": AGENT_OID,
                    "AGENT_USER_ID": AGENT_USER_ID,
                }.get(k),
            ),
        ):
            rc = consent_mod.main(
                ["--scopes", "Chat.Create,Chat.ReadWrite"]
            )
        assert rc == 0
        mock_requests.post.assert_called_once()


class TestPatchExistingConsent:
    """PATCH when grant exists but is missing scopes."""

    @patch.dict("os.environ", {}, clear=False)
    def test_patch_adds_missing(self):
        _load_module()

        existing_grant = {
            "id": GRANT_ID,
            "scope": "Chat.Create",
            "resourceId": GRAPH_SP_OID,
        }

        def fake_requests_method(method_name):
            def handler(url, **kwargs):
                if "servicePrincipals" in url and "appId" in url:
                    return _json_resp(
                        200, {"value": [{"id": GRAPH_SP_OID}]}
                    )
                if "oauth2PermissionGrants" in url and method_name == "get":
                    return _json_resp(200, {"value": [existing_grant]})
                if (
                    "oauth2PermissionGrants" in url
                    and method_name == "patch"
                ):
                    return _json_resp(204)
                return _json_resp(404)
            return handler

        mock_requests = MagicMock()
        mock_requests.get.side_effect = fake_requests_method("get")
        mock_requests.patch.side_effect = fake_requests_method("patch")

        with (
            patch.object(consent_mod, "requests", mock_requests),
            patch.object(
                consent_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(
                consent_mod,
                "get_state",
                side_effect=lambda k: {
                    "AGENT_OBJECT_ID": AGENT_OID,
                    "AGENT_USER_ID": AGENT_USER_ID,
                }.get(k),
            ),
        ):
            rc = consent_mod.main(
                ["--scopes", "Chat.Create,Mail.Read"]
            )
        assert rc == 0
        mock_requests.patch.assert_called_once()


class TestSkipWhenAllScopesPresent:
    """Skip when all requested scopes already granted."""

    @patch.dict("os.environ", {}, clear=False)
    def test_skip(self):
        _load_module()

        existing_grant = {
            "id": GRANT_ID,
            "scope": "Chat.Create Mail.Read",
            "resourceId": GRAPH_SP_OID,
        }

        def fake_requests_method(method_name):
            def handler(url, **kwargs):
                if "servicePrincipals" in url and "appId" in url:
                    return _json_resp(
                        200, {"value": [{"id": GRAPH_SP_OID}]}
                    )
                if "oauth2PermissionGrants" in url:
                    return _json_resp(200, {"value": [existing_grant]})
                return _json_resp(404)
            return handler

        mock_requests = MagicMock()
        mock_requests.get.side_effect = fake_requests_method("get")

        with (
            patch.object(consent_mod, "requests", mock_requests),
            patch.object(
                consent_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(
                consent_mod,
                "get_state",
                side_effect=lambda k: {
                    "AGENT_OBJECT_ID": AGENT_OID,
                    "AGENT_USER_ID": AGENT_USER_ID,
                }.get(k),
            ),
        ):
            rc = consent_mod.main(
                ["--scopes", "Chat.Create,Mail.Read"]
            )
        assert rc == 0
        mock_requests.post.assert_not_called()
        mock_requests.patch.assert_not_called()


class TestMissingState:
    """Error when agent IDs not in state."""

    @patch.dict("os.environ", {}, clear=False)
    def test_missing_state(self):
        _load_module()
        with (
            patch.object(
                consent_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(consent_mod, "get_state", return_value=None),
            patch.object(consent_mod, "requests", MagicMock()),
        ):
            rc = consent_mod.main(["--scopes", "Chat.Create"])
        assert rc == 1


class TestCustomResourceApp:
    """--resource-app-id resolves the right SP."""

    @patch.dict("os.environ", {}, clear=False)
    def test_custom_resource(self):
        _load_module()
        custom_app_id = "e406a681-f3d4-42a8-90b6-c2b029497af1"
        custom_sp_id = "storage-sp-eee"

        def fake_requests_method(method_name):
            def handler(url, **kwargs):
                if "servicePrincipals" in url and custom_app_id in url:
                    return _json_resp(
                        200, {"value": [{"id": custom_sp_id}]}
                    )
                if "oauth2PermissionGrants" in url and method_name == "get":
                    return _json_resp(200, {"value": []})
                if "oauth2PermissionGrants" in url and method_name == "post":
                    return _json_resp(201, {})
                return _json_resp(404)
            return handler

        mock_requests = MagicMock()
        mock_requests.get.side_effect = fake_requests_method("get")
        mock_requests.post.side_effect = fake_requests_method("post")

        with (
            patch.object(consent_mod, "requests", mock_requests),
            patch.object(
                consent_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(
                consent_mod,
                "get_state",
                side_effect=lambda k: {
                    "AGENT_OBJECT_ID": AGENT_OID,
                    "AGENT_USER_ID": AGENT_USER_ID,
                }.get(k),
            ),
        ):
            rc = consent_mod.main(
                [
                    "--scopes", "user_impersonation",
                    "--resource-app-id", custom_app_id,
                ]
            )
        assert rc == 0


class TestCLIHelp:
    """CLI --help exits 0."""

    @patch.dict("os.environ", {}, clear=False)
    def test_help(self):
        _load_module()
        with pytest.raises(SystemExit) as exc:
            consent_mod.main(["--help"])
        assert exc.value.code == 0


class TestScopesRequired:
    """--scopes is required."""

    @patch.dict("os.environ", {}, clear=False)
    def test_no_scopes(self):
        _load_module()
        with pytest.raises(SystemExit):
            consent_mod.main([])
