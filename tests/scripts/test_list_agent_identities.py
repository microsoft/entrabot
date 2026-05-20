"""Tests for scripts/list_agent_identities.py."""

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
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "list_agent_identities.py"
)
spec = importlib.util.spec_from_file_location("list_agent_identities", _SCRIPT)
list_mod = importlib.util.module_from_spec(spec)
sys.modules["list_agent_identities"] = list_mod

# Stub entra_provisioning before the module is exec'd
_prov_stub = MagicMock()
_prov_stub.get_existing_graph_token.return_value = "tok-test"
_prov_stub.get_state.return_value = None
_prov_stub.ProvisionerBootstrapError = type(
    "ProvisionerBootstrapError", (Exception,), {}
)
sys.modules.setdefault("entra_provisioning", _prov_stub)


def _load_module():
    spec.loader.exec_module(list_mod)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BLUEPRINT_APP_ID = "bp-app-aaa"
IDENTITY_1 = {
    "id": "id-111",
    "appId": "app-111",
    "displayName": "Agent Identity 1",
    "agentIdentityBlueprintId": BLUEPRINT_APP_ID,
}
IDENTITY_2 = {
    "id": "id-222",
    "appId": "app-222",
    "displayName": "Agent Identity 2",
    "agentIdentityBlueprintId": BLUEPRINT_APP_ID,
}
OTHER_IDENTITY = {
    "id": "id-999",
    "appId": "app-999",
    "displayName": "Other Agent",
    "agentIdentityBlueprintId": "other-bp",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestListIdentities:
    """List all Agent Identities under a Blueprint."""

    @patch.dict("os.environ", {}, clear=False)
    def test_list(self, capsys):
        _load_module()
        all_identities = [IDENTITY_1, IDENTITY_2, OTHER_IDENTITY]

        with (
            patch.object(
                list_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(
                list_mod, "get_state", return_value=BLUEPRINT_APP_ID
            ),
            patch.object(
                list_mod,
                "graph_collection_values",
                return_value=all_identities,
            ),
        ):
            rc = list_mod.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Agent Identity 1" in out
        assert "Agent Identity 2" in out
        # OTHER_IDENTITY should be filtered out
        assert "Other Agent" not in out


class TestExplicitBlueprintId:
    """--blueprint-app-id overrides state."""

    @patch.dict("os.environ", {}, clear=False)
    def test_explicit_bp(self, capsys):
        _load_module()
        explicit_bp = "explicit-bp-id"
        identity = {
            "id": "id-333",
            "appId": "app-333",
            "displayName": "Explicit Agent",
            "agentIdentityBlueprintId": explicit_bp,
        }

        with (
            patch.object(
                list_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(list_mod, "get_state", return_value="ignored"),
            patch.object(
                list_mod,
                "graph_collection_values",
                return_value=[identity, OTHER_IDENTITY],
            ),
        ):
            rc = list_mod.main(["--blueprint-app-id", explicit_bp])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Explicit Agent" in out
        assert "Other Agent" not in out


class TestNoBlueprintId:
    """Error when no Blueprint ID available."""

    @patch.dict("os.environ", {}, clear=False)
    def test_no_bp(self):
        _load_module()
        with (
            patch.object(
                list_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(list_mod, "get_state", return_value=None),
        ):
            rc = list_mod.main([])
        assert rc == 1


class TestNoIdentitiesFound:
    """Empty result when no identities match."""

    @patch.dict("os.environ", {}, clear=False)
    def test_empty(self, capsys):
        _load_module()
        with (
            patch.object(
                list_mod, "get_existing_graph_token", return_value="tok"
            ),
            patch.object(
                list_mod, "get_state", return_value=BLUEPRINT_APP_ID
            ),
            patch.object(
                list_mod,
                "graph_collection_values",
                return_value=[OTHER_IDENTITY],
            ),
        ):
            rc = list_mod.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No Agent Identities" in out


class TestCLIHelp:
    """CLI --help exits 0."""

    @patch.dict("os.environ", {}, clear=False)
    def test_help(self):
        _load_module()
        with pytest.raises(SystemExit) as exc:
            list_mod.main(["--help"])
        assert exc.value.code == 0
