"""Tests for the Blueprint-scoped lookup logic in create_entra_agent_ids.py.

The wire fix being guarded: when a tenant hosts multiple EntraClaw
Blueprints, every Blueprint gets its own Agent Identity SP with the
same display name (``EntraClaw Agent - <host>``). A lookup that
filters only on displayName can return the wrong Blueprint's
identity. These tests pin the fix: both ``find_existing_agent_identity``
and ``find_existing_agent_user`` must scope their results to the
intended Blueprint.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "create_entra_agent_ids.py"


@pytest.fixture
def agent_ids_module():
    spec = importlib.util.spec_from_file_location("create_entra_agent_ids", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["create_entra_agent_ids"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("create_entra_agent_ids", None)


def _resp(status: int, body: dict) -> SimpleNamespace:
    """Build a minimal object that quacks like requests.Response."""
    return SimpleNamespace(status_code=status, json=lambda: body)


BLUEPRINT_OURS = "9bfb75b3-e65f-4e56-bdbe-3ed213135c3b"
BLUEPRINT_OTHER = "11111111-1111-1111-1111-111111111111"
DISPLAY_NAME = "EntraClaw Agent - test-host"


def _sp(app_id: str, blueprint: str) -> dict:
    return {
        "id": app_id,
        "appId": app_id,
        "displayName": DISPLAY_NAME,
        "agentIdentityBlueprintId": blueprint,
        "@odata.type": "#microsoft.graph.agentIdentity",
    }


class TestFindExistingAgentIdentity:
    def test_returns_sp_matching_target_blueprint(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two SPs share a display name — only the one under our Blueprint is returned."""

        def fake_graph_request(method, path, token, **kw):
            return _resp(
                200,
                {
                    "value": [
                        _sp("eba51655-0aed-4a79-a5f2-7167ec9b8fa0", BLUEPRINT_OURS),
                        _sp("22222222-2222-2222-2222-222222222222", BLUEPRINT_OTHER),
                    ]
                },
            )

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        result = agent_ids_module.find_existing_agent_identity(
            token="tok",
            display_name=DISPLAY_NAME,
            blueprint_app_id=BLUEPRINT_OURS,
        )
        assert result is not None
        assert result["appId"] == "eba51655-0aed-4a79-a5f2-7167ec9b8fa0"
        assert result["agentIdentityBlueprintId"] == BLUEPRINT_OURS

    def test_rejects_stored_app_id_from_other_blueprint(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """stored_app_id from a different Blueprint must not be trusted.

        Regression for the 2026-04-19 cross-contamination: state held an
        AGENT_ID from an old Blueprint and the lookup returned it
        verbatim, silently pinning the chain to the wrong Blueprint.
        """

        def fake_graph_request(method, path, token, **kw):
            # stored_app_id query returns the old-Blueprint SP
            if "eq '52dff96e" in path:
                return _resp(
                    200,
                    {
                        "value": [
                            _sp("22222222-2222-2222-2222-222222222222", BLUEPRINT_OTHER),
                        ]
                    },
                )
            # displayName fallback returns nothing under our Blueprint
            return _resp(200, {"value": []})

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        result = agent_ids_module.find_existing_agent_identity(
            token="tok",
            display_name=DISPLAY_NAME,
            blueprint_app_id=BLUEPRINT_OURS,
            stored_app_id="22222222-2222-2222-2222-222222222222",
        )
        assert result is None
        # Warning printed so the operator sees why state got rejected
        assert "parented by a different Blueprint" in capsys.readouterr().out

    def test_returns_none_when_no_sp_under_our_blueprint(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_graph_request(method, path, token, **kw):
            return _resp(
                200,
                {
                    "value": [
                        _sp("22222222-2222-2222-2222-222222222222", BLUEPRINT_OTHER),
                    ]
                },
            )

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)

        result = agent_ids_module.find_existing_agent_identity(
            token="tok",
            display_name=DISPLAY_NAME,
            blueprint_app_id=BLUEPRINT_OURS,
        )
        assert result is None


class TestFindExistingAgentUser:
    _OUR_AI = "eba51655-0aed-4a79-a5f2-7167ec9b8fa0"
    _OTHER_AI = "22222222-2222-2222-2222-222222222222"

    def test_stored_user_under_our_agent_identity_is_trusted(
        self, agent_ids_module, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_graph_request(method, path, token, **kw):
            return _resp(
                200,
                {
                    "id": "9e5d2c48-ca9c-4298-80cb-18fc382aa7b2",
                    "userPrincipalName": "entraclaw-agent-sati-agent@werner.ac",
                    "identityParentId": self._OUR_AI,
                },
            )

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(
            agent_ids_module,
            "get_state",
            lambda k: "9e5d2c48-ca9c-4298-80cb-18fc382aa7b2" if k == "AGENT_USER_ID" else None,
        )

        result = agent_ids_module.find_existing_agent_user(
            token="tok",
            agent_identity_obj_id=self._OUR_AI,
        )
        assert result is not None
        assert result["identityParentId"] == self._OUR_AI

    def test_stored_user_under_other_agent_identity_is_rejected(
        self,
        agent_ids_module,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A stored AGENT_USER_ID whose user is parented by a different
        Agent Identity must not be returned — otherwise callers derive
        the wrong chain downstream."""
        calls: list[str] = []

        def fake_graph_request(method, path, token, **kw):
            calls.append(path)
            if path.startswith("/users/9e5d2c48"):
                # Fetched stored user — but it's under a DIFFERENT Agent Identity
                return _resp(
                    200,
                    {
                        "id": "9e5d2c48-ca9c-4298-80cb-18fc382aa7b2",
                        "identityParentId": self._OTHER_AI,
                    },
                )
            # Fallback identityParentId filter finds nothing under OUR AI
            return _resp(200, {"value": []})

        monkeypatch.setattr(agent_ids_module, "graph_request", fake_graph_request)
        monkeypatch.setattr(
            agent_ids_module,
            "get_state",
            lambda k: "9e5d2c48-ca9c-4298-80cb-18fc382aa7b2" if k == "AGENT_USER_ID" else None,
        )

        result = agent_ids_module.find_existing_agent_user(
            token="tok",
            agent_identity_obj_id=self._OUR_AI,
        )
        assert result is None
        assert "parented by a different Agent Identity" in capsys.readouterr().out
        # Verify both the stored lookup AND the fallback filter ran
        assert any(p.startswith("/users/9e5d2c48") for p in calls)
        assert any("identityParentId eq" in p for p in calls)
