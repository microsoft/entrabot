"""Tests for scripts/assign_agent_user_licenses.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading boilerplate (same pattern as other script tests)
# ---------------------------------------------------------------------------
_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "assign_agent_user_licenses.py"
)
spec = importlib.util.spec_from_file_location("assign_agent_user_licenses", _SCRIPT)
lic_mod = importlib.util.module_from_spec(spec)
sys.modules["assign_agent_user_licenses"] = lic_mod

# Stub entra_provisioning before the module is exec'd
_prov_stub = MagicMock()
_prov_stub.get_existing_graph_token.return_value = "tok-test"
_prov_stub.get_state.return_value = None
_prov_stub.set_state = MagicMock()
_prov_stub.ProvisionerBootstrapError = type(
    "ProvisionerBootstrapError", (Exception,), {}
)
sys.modules["entra_provisioning"] = _prov_stub


def _load_module():
    spec.loader.exec_module(lic_mod)


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else str(body)
    r.json.return_value = body or {}
    return r


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AGENT_USER_ID = "agent-user-aaa"
TEAMS_SKU_ID = "sku-teams-111"
COPILOT_SKU_ID = "sku-copilot-222"

SUBSCRIBED_SKUS = {
    "value": [
        {
            "skuId": TEAMS_SKU_ID,
            "skuPartNumber": "ENTERPRISEPACK",
            "prepaidUnits": {"enabled": 10},
            "consumedUnits": 2,
        },
        {
            "skuId": COPILOT_SKU_ID,
            "skuPartNumber": "Microsoft_365_Copilot",
            "prepaidUnits": {"enabled": 5},
            "consumedUnits": 1,
        },
    ]
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestListAvailable:
    """--list-available prints SKUs and exits."""

    @patch.dict("os.environ", {}, clear=False)
    def test_list_available(self):
        _load_module()
        with (
            patch.object(
                lic_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(
                lic_mod, "graph_request",
                return_value=_json_resp(200, SUBSCRIBED_SKUS),
            ),
        ):
            rc = lic_mod.main(["--list-available"])
        assert rc == 0


class TestAssignTeamsLicense:
    """Assign a Teams-capable license when user has none."""

    @patch.dict("os.environ", {}, clear=False)
    def test_assign_teams(self):
        _load_module()
        existing_licenses = {"assignedLicenses": []}

        def fake_graph(method, path, token, json_body=None, **kw):
            if "/subscribedSkus" in path:
                return _json_resp(200, SUBSCRIBED_SKUS)
            if "assignedLicenses" in path and method == "GET":
                return _json_resp(200, existing_licenses)
            if "assignLicense" in path:
                return _json_resp(200, {})
            if method == "PATCH" and "/users/" in path:
                return _json_resp(204)
            return _json_resp(404)

        with (
            patch.object(
                lic_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(lic_mod, "get_state", return_value=AGENT_USER_ID),
            patch.object(lic_mod, "set_state"),
            patch.object(lic_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = lic_mod.main([])
        assert rc == 0


class TestAlreadyHasLicense:
    """Skip assignment when user already has both licenses."""

    @patch.dict("os.environ", {}, clear=False)
    def test_skip_when_licensed(self):
        _load_module()
        existing_licenses = {
            "assignedLicenses": [
                {"skuId": TEAMS_SKU_ID},
                {"skuId": COPILOT_SKU_ID},
            ]
        }

        def fake_graph(method, path, token, json_body=None, **kw):
            if "/subscribedSkus" in path:
                return _json_resp(200, SUBSCRIBED_SKUS)
            if "assignedLicenses" in path and method == "GET":
                return _json_resp(200, existing_licenses)
            return _json_resp(404)

        with (
            patch.object(
                lic_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(lic_mod, "get_state", return_value=AGENT_USER_ID),
            patch.object(lic_mod, "set_state"),
            patch.object(lic_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = lic_mod.main([])
        # Should succeed (skip, not error)
        assert rc == 0


class TestMissingAgentUser:
    """Error when AGENT_USER_ID is not in state."""

    @patch.dict("os.environ", {}, clear=False)
    def test_missing_user(self):
        _load_module()
        with (
            patch.object(
                lic_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(lic_mod, "get_state", return_value=None),
        ):
            rc = lic_mod.main([])
        assert rc == 1


class TestSpecificSku:
    """--sku assigns a specific SKU by part number."""

    @patch.dict("os.environ", {}, clear=False)
    def test_assign_specific(self):
        _load_module()

        def fake_graph(method, path, token, json_body=None, **kw):
            if "/subscribedSkus" in path:
                return _json_resp(200, SUBSCRIBED_SKUS)
            if "assignedLicenses" in path and method == "GET":
                return _json_resp(200, {"assignedLicenses": []})
            if "assignLicense" in path:
                assert json_body["addLicenses"][0]["skuId"] == COPILOT_SKU_ID
                return _json_resp(200, {})
            if method == "PATCH" and "/users/" in path:
                return _json_resp(204)
            return _json_resp(404)

        with (
            patch.object(
                lic_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(lic_mod, "get_state", return_value=AGENT_USER_ID),
            patch.object(lic_mod, "set_state"),
            patch.object(lic_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = lic_mod.main(["--sku", "Microsoft_365_Copilot"])
        assert rc == 0


class TestCLIHelp:
    """CLI --help exits 0."""

    @patch.dict("os.environ", {}, clear=False)
    def test_help(self):
        _load_module()
        with pytest.raises(SystemExit) as exc:
            lic_mod.main(["--help"])
        assert exc.value.code == 0
