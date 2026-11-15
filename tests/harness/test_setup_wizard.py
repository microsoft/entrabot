"""Tests for `entrabot init` helpers (idempotent re-run env layering)."""

import os
from unittest.mock import Mock

import pytest

from entrabot.harness import setup as sw
from entrabot.harness.config import globalcfg


@pytest.fixture(autouse=True)
def isolate_setup_environment(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("ENTRABOT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))


# ── _apply_existing_env (idempotent re-run / resume) ──────────────────────────
def test_apply_existing_env_layers_agent_over_global(tmp_path, monkeypatch):
    """Re-running `init` in a provisioned dir must load global (tenant/blueprint) + this dir's
    agent identity into the process so the connection re-test and recipient edit work."""
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    for k in ("ENTRABOT_TENANT_ID", "ENTRABOT_AGENT_USER_UPN", "ENTRABOT_AGENT_ID"):
        monkeypatch.delenv(k, raising=False)
    globalcfg.write_env(
        globalcfg.global_env_path(),
        {"ENTRABOT_TENANT_ID": "tid", "ENTRABOT_BLUEPRINT_APP_ID": "bp"},
    )
    agentdir = str(tmp_path / "proj")
    globalcfg.write_env(
        globalcfg.agent_env_path(agentdir),
        {"ENTRABOT_AGENT_ID": "ag", "ENTRABOT_AGENT_USER_UPN": "bot@x.onmicrosoft.com"},
    )

    sw._apply_existing_env(agentdir)
    assert os.environ["ENTRABOT_TENANT_ID"] == "tid"  # from global
    assert os.environ["ENTRABOT_AGENT_USER_UPN"] == "bot@x.onmicrosoft.com"  # from per-agent


def test_resume_existing_agent_rejects_conflicting_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    globalcfg.write_env(globalcfg.global_env_path(), {"ENTRABOT_TENANT_ID": "shared-tenant"})
    globalcfg.write_env(globalcfg.agent_env_path(str(tmp_path)), {
        "ENTRABOT_TENANT_ID": "another-tenant", "ENTRABOT_AGENT_USER_UPN": "agent@example.test",
    })

    with pytest.raises(ValueError, match="Conflicting ENTRABOT_TENANT_ID"):
        sw._apply_existing_env(str(tmp_path))


@pytest.mark.parametrize("platform,args", [
    ("windows", ("setup-windows.ps1", "-NewChain", "-UpnSuffix", "fixture")),
    ("linux", ("setup.sh", "--new", "--with-upn-suffix=fixture")),
])
def test_wizard_bootstraps_only_new_chain_once(monkeypatch, platform, args):
    from entrabot.harness.setup import provisioning

    script = Mock(return_value=["fixture-command"])
    run = Mock(return_value=0)
    monkeypatch.setattr(provisioning, "_ps", script)
    monkeypatch.setattr(provisioning, "_sh", script)
    monkeypatch.setattr(provisioning, "_run", run)

    assert provisioning._run_setup(platform, "fixture")
    script.assert_called_once_with(*args)
    run.assert_called_once_with(["fixture-command"])


def test_wizard_uses_shared_persistence_and_agent_writer(monkeypatch, tmp_path):
    from entrabot.harness.setup import provisioning

    source = tmp_path / ".env"
    generated = {
        "ENTRABOT_TENANT_ID": "fixture-tenant",
        "ENTRABOT_BLUEPRINT_APP_ID": "fixture-blueprint",
        "ENTRABOT_BLUEPRINT_OBJECT_ID": "fixture-object",
        "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "fixture-cert",
        "ENTRABOT_AGENT_ID": "fixture-agent",
        "ENTRABOT_AGENT_USER_UPN": "fixture@example.test",
    }
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    for key in generated:
        monkeypatch.delenv(key, raising=False)
    globalcfg.write_env(str(source), generated)
    monkeypatch.setattr(provisioning, "_clone_root", lambda: str(tmp_path))
    persist = Mock(return_value=str(tmp_path / "home" / "global.env"))
    write_agent = Mock()
    monkeypatch.setattr(globalcfg, "persist_global_from_env", persist)
    monkeypatch.setattr(provisioning, "_write_agent_env", write_agent)

    assert provisioning._persist_split(str(tmp_path / "agent"), "Fixture")
    persist.assert_called_once_with(str(source))
    write_agent.assert_called_once_with(str(tmp_path / "agent"), "Fixture", {
        "ENTRABOT_AGENT_ID": "fixture-agent", "ENTRABOT_AGENT_USER_UPN": "fixture@example.test",
    })


def test_wheel_init_reports_missing_scripts_before_attempting_setup(
    monkeypatch, tmp_path, capsys,
):
    from entrabot.harness.setup import provisioning

    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(provisioning, "_provisioning_available", lambda: False)
    prepare = Mock(side_effect=AssertionError("wheel has no provisioning scripts"))
    monkeypatch.setattr(provisioning, "_prepare_new_chain", prepare)

    assert not provisioning._provision_identity("windows", str(tmp_path), "Fixture", Mock())
    prepare.assert_not_called()
    assert "clone" in capsys.readouterr().out.lower()


def test_agent_writer_applies_same_isolation_rules_as_runtime(monkeypatch, tmp_path):
    from entrabot import config
    from entrabot.harness.setup import provisioning

    for key in globalcfg.AGENT_KEYS:
        monkeypatch.setenv(key, "true" if "ENABLED" in key else "primary")
    provisioning._write_agent_env(str(tmp_path), "Second", {
        "ENTRABOT_AGENT_ID": "second", "ENTRABOT_AGENT_USER_UPN": "second@example.test",
    })

    resolved = config.get_config()
    assert resolved.agent_id == "second"
    assert resolved.agent_user_id is None
    assert not resolved.a365_export_enabled
