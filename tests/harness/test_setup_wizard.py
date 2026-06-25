"""Tests for `entrabot init` helpers (idempotent re-run env layering)."""

from entrabot.harness import globalcfg
from entrabot.harness import setup_wizard as sw


# ── _apply_existing_env (idempotent re-run / resume) ──────────────────────────
def test_apply_existing_env_layers_agent_over_global(tmp_path, monkeypatch):
    """Re-running `init` in a provisioned dir must load global (tenant/blueprint) + this dir's
    agent identity into the process so the connection re-test and recipient edit work."""
    import os

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
