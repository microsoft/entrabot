"""Additional agents must not inherit a primary agent's UPN at import time."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

from entrabot.harness.config import globalcfg
from entrabot.harness.setup import provisioning

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def test_additional_agent_ignores_primary_upn_captured_during_import(monkeypatch, tmp_path):
    monkeypatch.setenv("ENTRABOT_AGENT_USER_UPN", "primary@old.example")
    monkeypatch.setenv("_ENTRABOT_UPN_SUFFIX", "second")
    monkeypatch.delenv("ENTRABOT_NEW_CHAIN", raising=False)
    identity_spec = importlib.util.spec_from_file_location(
        "create_entra_agent_ids", SCRIPTS / "create_entra_agent_ids.py"
    )
    identities = importlib.util.module_from_spec(identity_spec)
    monkeypatch.setitem(sys.modules, identity_spec.name, identities)
    identity_spec.loader.exec_module(identities)
    spec = importlib.util.spec_from_file_location("add_agent", SCRIPTS / "add_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts" / "add_agent.py"))

    monkeypatch.setattr(identities, "get_existing_graph_token", lambda: "fixture-token")
    monkeypatch.setattr(identities, "create_blueprint", lambda token: ("blueprint", "bp-object"))
    monkeypatch.setattr(identities, "create_agent_identity", lambda *a: ("second", "second-object"))
    monkeypatch.setattr(
        identities.requests, "get",
        Mock(return_value=Mock(status_code=200, json=lambda: {
            "value": [{"id": "new.example", "isDefault": True, "isVerified": True}],
        })),
    )
    upns = []

    def create_user(token, parent, *, explicit_upn=None):
        upn = identities._agent_user_upn(token, explicit_upn=explicit_upn)
        upns.append(upn)
        return "second-user", upn

    monkeypatch.setattr(identities, "create_agent_user", create_user)
    for name in (
        "grant_agent_identity_app_permissions", "grant_agent_user_consent",
        "grant_agent_user_storage_consent", "assign_license_to_agent_user",
    ):
        monkeypatch.setattr(identities, name, Mock())

    assert module.main() == 0
    assert upns == ["entrabot-agent-second@new.example"]
    assert identities._EXPLICIT_AGENT_USER_UPN == "primary@old.example"


def test_wizard_removes_primary_identity_from_additional_agent_environment(monkeypatch):
    for key in globalcfg.AGENT_KEYS:
        monkeypatch.setenv(key, "primary-setting")
    monkeypatch.setattr(provisioning, "_scripts_dir", lambda: str(SCRIPTS))
    monkeypatch.setattr(provisioning, "_venv_python", lambda: sys.executable)
    result = Mock(returncode=0, stderr="", stdout='AGENT_JSON={"ENTRABOT_AGENT_USER_UPN":"second"}')
    run = Mock(return_value=result)
    monkeypatch.setattr(provisioning.subprocess, "run", run)

    assert provisioning._run_add_agent("Second", "second") is not None

    child_env = run.call_args.kwargs["env"]
    assert not any(key in child_env for key in globalcfg.AGENT_KEYS)
    assert child_env["_ENTRABOT_UPN_SUFFIX"] == "second"
