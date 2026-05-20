"""Tests for scripts/list_sponsors.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "list_sponsors.py"
)
spec = importlib.util.spec_from_file_location("list_sponsors", _SCRIPT)
spons_mod = importlib.util.module_from_spec(spec)
sys.modules["list_sponsors"] = spons_mod

# Stub entra_provisioning
_prov_stub = MagicMock()
_prov_stub.get_existing_graph_token.return_value = "tok-test"
_prov_stub.get_state.return_value = None
_prov_stub.ProvisionerBootstrapError = type(
    "ProvisionerBootstrapError", (Exception,), {}
)
sys.modules.setdefault("entra_provisioning", _prov_stub)


def _load_module():
    spec.loader.exec_module(spons_mod)


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else json.dumps(body)
    r.json.return_value = body or {}
    return r


# ---------------------------------------------------------------------------
AGENT_OID = "agent-obj-111"
SPONSORS = [
    {
        "id": "s1",
        "displayName": "Alice",
        "userPrincipalName": "alice@example.com",
        "mail": "alice@example.com",
    },
    {
        "id": "s2",
        "displayName": "Bob",
        "userPrincipalName": "bob@example.com",
        "mail": None,
    },
]


class TestListSponsors:
    """List sponsors for the Agent Identity."""

    @patch.dict("os.environ", {}, clear=False)
    def test_list(self, capsys):
        _load_module()

        def fake_graph(method, path, token, **kw):
            if "/sponsors" in path:
                return _json_resp(200, {"value": SPONSORS})
            return _json_resp(404)

        with (
            patch.object(spons_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(spons_mod, "get_state", return_value=AGENT_OID),
            patch.object(spons_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = spons_mod.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Alice" in out
        assert "Bob" in out


class TestJsonOutput:
    """--json produces valid JSON."""

    @patch.dict("os.environ", {}, clear=False)
    def test_json(self, capsys):
        _load_module()

        def fake_graph(method, path, token, **kw):
            if "/sponsors" in path:
                return _json_resp(200, {"value": SPONSORS})
            return _json_resp(404)

        with (
            patch.object(spons_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(spons_mod, "get_state", return_value=AGENT_OID),
            patch.object(spons_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = spons_mod.main(["--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 2
        assert data[0]["displayName"] == "Alice"


class TestExplicitOid:
    """--agent-object-id overrides state."""

    @patch.dict("os.environ", {}, clear=False)
    def test_explicit(self, capsys):
        _load_module()
        explicit_oid = "explicit-oid-999"

        def fake_graph(method, path, token, **kw):
            assert explicit_oid in path
            return _json_resp(200, {"value": SPONSORS[:1]})

        with (
            patch.object(spons_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(spons_mod, "get_state", return_value=None),
            patch.object(spons_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = spons_mod.main(["--agent-object-id", explicit_oid])
        assert rc == 0


class TestNoAgentOid:
    """Error when no Agent Object ID available."""

    @patch.dict("os.environ", {}, clear=False)
    def test_missing(self):
        _load_module()
        with (
            patch.object(spons_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(spons_mod, "get_state", return_value=None),
        ):
            rc = spons_mod.main([])
        assert rc == 1


class TestNoSponsors:
    """Graceful output when no sponsors found."""

    @patch.dict("os.environ", {}, clear=False)
    def test_empty(self, capsys):
        _load_module()

        def fake_graph(method, path, token, **kw):
            return _json_resp(200, {"value": []})

        with (
            patch.object(spons_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(spons_mod, "get_state", return_value=AGENT_OID),
            patch.object(spons_mod, "graph_request", side_effect=fake_graph),
        ):
            rc = spons_mod.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No sponsors" in out or "0" in out


class TestCLIHelp:
    """CLI --help exits 0."""

    @patch.dict("os.environ", {}, clear=False)
    def test_help(self):
        _load_module()
        with pytest.raises(SystemExit) as exc:
            spons_mod.main(["--help"])
        assert exc.value.code == 0
