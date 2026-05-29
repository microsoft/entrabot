"""Tests for scripts/remove_agent_sponsor.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the script as a module
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "remove_agent_sponsor.py"
spec = importlib.util.spec_from_file_location("remove_agent_sponsor", _SCRIPT)
remove_mod = importlib.util.module_from_spec(spec)
sys.modules["remove_agent_sponsor"] = remove_mod


def _load_module():
    """(Re-)exec the module — must be called after patching entra_provisioning."""
    spec.loader.exec_module(remove_mod)


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else str(body)
    r.json.return_value = body or {}
    return r


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AGENT_OID = "agent-oid-111"
SPONSOR_OID = "sponsor-oid-222"
SPONSOR_EMAIL = "alice@example.com"
SPONSOR_NAME = "Alice"

SPONSORS_BEFORE = [
    {
        "id": SPONSOR_OID, "displayName": SPONSOR_NAME,
        "userPrincipalName": SPONSOR_EMAIL, "mail": SPONSOR_EMAIL,
    },
    {
        "id": "other-oid", "displayName": "Bob",
        "userPrincipalName": "bob@example.com", "mail": "bob@example.com",
    },
]

SPONSORS_AFTER = [
    {
        "id": "other-oid", "displayName": "Bob",
        "userPrincipalName": "bob@example.com", "mail": "bob@example.com",
    },
]


@pytest.fixture(autouse=True)
def _patch_provisioning():
    """Stub entra_provisioning so the script can import without .entrabot-state.json."""
    prov = MagicMock()
    prov.get_graph_token.return_value = "fake-token"
    prov.get_state.return_value = AGENT_OID
    prov.ProvisionerBootstrapError = type("ProvisionerBootstrapError", (Exception,), {})
    sys.modules["entra_provisioning"] = prov
    _load_module()
    yield prov
    sys.modules.pop("entra_provisioning", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRemoveSponsor:
    """Core remove-sponsor workflow."""

    @patch.object(remove_mod, "resolve_user_by_email", return_value=(SPONSOR_OID, SPONSOR_NAME))
    @patch.object(remove_mod, "requests")
    def test_happy_path(self, mock_requests, mock_resolve, capsys):
        """Resolve email → DELETE sponsor ref → print updated list."""
        mock_requests.get.side_effect = [
            _json_resp(200, {"value": SPONSORS_BEFORE}),  # list before
            _json_resp(200, {"value": SPONSORS_AFTER}),   # list after
        ]
        mock_requests.delete.return_value = _json_resp(204)

        rc = remove_mod.main(["prog", SPONSOR_EMAIL])
        assert rc == 0

        # DELETE was called with the right URL
        delete_call = mock_requests.delete.call_args
        assert AGENT_OID in delete_call[0][0]
        assert SPONSOR_OID in delete_call[0][0]

        out = capsys.readouterr().out
        assert "Removed" in out or "done" in out

    @patch.object(remove_mod, "resolve_user_by_email", return_value=(SPONSOR_OID, SPONSOR_NAME))
    @patch.object(remove_mod, "requests")
    def test_sponsor_not_in_list(self, mock_requests, mock_resolve, capsys):
        """DELETE returns 404 → the sponsor wasn't there."""
        mock_requests.get.return_value = _json_resp(200, {"value": SPONSORS_AFTER})
        mock_requests.delete.return_value = _json_resp(404, {"error": "not found"})

        rc = remove_mod.main(["prog", SPONSOR_EMAIL])
        assert rc == 0
        out = capsys.readouterr().out
        assert "not a sponsor" in out.lower() or "already" in out.lower()

    @patch.object(remove_mod, "resolve_user_by_email", side_effect=LookupError("no such user"))
    def test_user_not_found(self, mock_resolve, capsys):
        rc = remove_mod.main(["prog", "unknown@example.com"])
        assert rc == 1

    def test_no_args(self, capsys):
        rc = remove_mod.main(["prog"])
        assert rc == 2

    @patch.object(remove_mod, "resolve_user_by_email", return_value=(SPONSOR_OID, SPONSOR_NAME))
    @patch.object(remove_mod, "requests")
    def test_delete_failure(self, mock_requests, mock_resolve, capsys):
        """Non-204/404 from DELETE → error exit."""
        mock_requests.get.return_value = _json_resp(200, {"value": SPONSORS_BEFORE})
        mock_requests.delete.return_value = _json_resp(403, {"error": "forbidden"})

        rc = remove_mod.main(["prog", SPONSOR_EMAIL])
        assert rc == 1

    def test_missing_agent_object_id(self, _patch_provisioning, capsys):
        """If AGENT_OBJECT_ID is not in state → error."""
        _patch_provisioning.get_state.return_value = None
        rc = remove_mod.main(["prog", SPONSOR_EMAIL])
        assert rc == 1


class TestWithExplicitAgentOid:
    """Pass --agent-object-id to override state."""

    @patch.object(remove_mod, "resolve_user_by_email", return_value=(SPONSOR_OID, SPONSOR_NAME))
    @patch.object(remove_mod, "requests")
    def test_explicit_oid(self, mock_requests, mock_resolve, capsys):
        mock_requests.get.return_value = _json_resp(200, {"value": []})
        mock_requests.delete.return_value = _json_resp(204)

        rc = remove_mod.main(["prog", SPONSOR_EMAIL, "--agent-object-id", "custom-oid"])
        assert rc == 0

        delete_url = mock_requests.delete.call_args[0][0]
        assert "custom-oid" in delete_url
