"""Tests for scripts/remove_agent_user_licenses.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the script as a module
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "remove_agent_user_licenses.py"
spec = importlib.util.spec_from_file_location("remove_agent_user_licenses", _SCRIPT)
lic_mod = importlib.util.module_from_spec(spec)
sys.modules["remove_agent_user_licenses"] = lic_mod


def _load_module():
    spec.loader.exec_module(lic_mod)


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else str(body)
    r.json.return_value = body or {}
    return r


AGENT_USER_UPN = "agent@example.com"
AGENT_USER_ID = "user-oid-111"

SKU_1 = "sku-aaa"
SKU_2 = "sku-bbb"

USER_WITH_LICENSES = {
    "id": AGENT_USER_ID,
    "userPrincipalName": AGENT_USER_UPN,
    "assignedLicenses": [
        {"skuId": SKU_1, "disabledPlans": []},
        {"skuId": SKU_2, "disabledPlans": []},
    ],
    "licenseAssignmentStates": [
        {"skuId": SKU_1, "assignedByGroup": None},
        {"skuId": SKU_2, "assignedByGroup": "group-ggg"},
    ],
}


@pytest.fixture(autouse=True)
def _patch_provisioning():
    prov = MagicMock()
    prov.get_graph_token.return_value = "fake-token"
    prov.get_state.side_effect = lambda k: {
        "AGENT_USER_UPN": AGENT_USER_UPN,
    }.get(k)
    prov.ProvisionerBootstrapError = type("ProvisionerBootstrapError", (Exception,), {})
    sys.modules["entra_provisioning"] = prov
    _load_module()
    yield prov
    sys.modules.pop("entra_provisioning", None)


import entraclaw.graph_helpers as _graph_helpers_mod  # noqa: E402


class TestRemoveAllDirectLicenses:
    @patch.object(_graph_helpers_mod, "graph_request")
    @patch.object(lic_mod, "graph_request")
    def test_removes_direct_skips_group(self, mock_gr, mock_gh_gr, capsys):
        """Remove directly-assigned licenses, skip group-inherited."""
        # Lookup user
        mock_gr.side_effect = [
            _json_resp(200, {"value": [USER_WITH_LICENSES]}),  # lookup
            _json_resp(200),  # assignLicense POST
        ]
        mock_gh_gr.side_effect = mock_gr.side_effect

        rc = lic_mod.main(["prog", "--all"])
        assert rc == 0

        # The POST body should only contain the directly-assigned SKU
        post_call = mock_gr.call_args_list[-1]
        body = post_call[0][3]  # json_body positional arg
        assert SKU_1 in body["removeLicenses"]
        assert SKU_2 not in body["removeLicenses"]

        out = capsys.readouterr().out
        assert "group-inherited" in out.lower() or "group" in out.lower()


class TestRemoveSpecificSku:
    @patch.object(_graph_helpers_mod, "graph_request")
    @patch.object(lic_mod, "graph_request")
    def test_remove_by_sku_id(self, mock_gr, mock_gh_gr, capsys):
        mock_gr.side_effect = [
            _json_resp(200, {"value": [USER_WITH_LICENSES]}),
            _json_resp(200),
        ]
        mock_gh_gr.side_effect = mock_gr.side_effect

        rc = lic_mod.main(["prog", "--sku-id", SKU_1])
        assert rc == 0

        post_call = mock_gr.call_args_list[-1]
        body = post_call[0][3]
        assert body["removeLicenses"] == [SKU_1]


class TestNoLicenses:
    @patch.object(_graph_helpers_mod, "graph_request")
    @patch.object(lic_mod, "graph_request")
    def test_no_licenses(self, mock_gr, mock_gh_gr, capsys):
        user = {**USER_WITH_LICENSES, "assignedLicenses": [], "licenseAssignmentStates": []}
        mock_gr.return_value = _json_resp(200, {"value": [user]})
        mock_gh_gr.return_value = mock_gr.return_value

        rc = lic_mod.main(["prog", "--all"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no" in out.lower() and "license" in out.lower()


class TestUserNotFound:
    @patch.object(_graph_helpers_mod, "graph_request")
    @patch.object(lic_mod, "graph_request")
    def test_user_not_found(self, mock_gr, mock_gh_gr, capsys):
        mock_gr.return_value = _json_resp(200, {"value": []})
        mock_gh_gr.return_value = mock_gr.return_value

        rc = lic_mod.main(["prog", "--all"])
        assert rc == 1


class TestCLIArgs:
    def test_help(self, capsys):
        rc = lic_mod.main(["prog", "--help"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "usage: remove_agent_user_licenses.py" in out

    def test_requires_all_or_sku(self, capsys):
        """Must provide --all or --sku-id."""
        rc = lic_mod.main(["prog"])
        assert rc == 2

    def test_missing_upn(self, _patch_provisioning, capsys):
        _patch_provisioning.get_state.return_value = None
        rc = lic_mod.main(["prog", "--all"])
        assert rc == 1
