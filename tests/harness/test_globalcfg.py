import os

import pytest

from entrabot.harness import globalcfg


def _combined():
    return {
        "ENTRABOT_TENANT_ID": "tid-1",
        "ENTRABOT_BLUEPRINT_APP_ID": "bp-app",
        "ENTRABOT_BLUEPRINT_OBJECT_ID": "bp-obj",
        "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "thumb",
        "ENTRABOT_BLUEPRINT_KSP": "ksp",
        "ENTRABOT_KEEP_MEMORY_LOCAL": "true",  # runtime pref → global, never dropped
        "ENTRABOT_AGENT_ID": "ag-app",
        "ENTRABOT_AGENT_OBJECT_ID": "ag-obj",
        "ENTRABOT_AGENT_USER_ID": "ag-user",
        "ENTRABOT_AGENT_USER_UPN": "bot@x.onmicrosoft.com",
    }


def test_split_partitions_global_vs_agent():
    glob, agent = globalcfg.split(_combined())
    assert agent == {
        "ENTRABOT_AGENT_ID": "ag-app",
        "ENTRABOT_AGENT_OBJECT_ID": "ag-obj",
        "ENTRABOT_AGENT_USER_ID": "ag-user",
        "ENTRABOT_AGENT_USER_UPN": "bot@x.onmicrosoft.com",
    }
    assert glob["ENTRABOT_TENANT_ID"] == "tid-1"
    assert glob["ENTRABOT_BLUEPRINT_APP_ID"] == "bp-app"
    # no agent key leaks into global; nothing dropped
    assert not any(k in glob for k in globalcfg.AGENT_KEYS)
    assert glob["ENTRABOT_KEEP_MEMORY_LOCAL"] == "true"


def test_env_roundtrip(tmp_path):
    p = str(tmp_path / "x.env")
    globalcfg.write_env(p, {"B": "2", "A": "1"}, header="hi")
    text = open(p).read()
    assert text.splitlines()[0] == "# hi"
    assert globalcfg.read_env(p) == {"A": "1", "B": "2"}


def test_global_exists_and_blueprint(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    assert globalcfg.global_exists() is False
    glob, _ = globalcfg.split(_combined())
    globalcfg.write_env(globalcfg.global_env_path(), glob)
    assert globalcfg.global_exists() is True
    assert globalcfg.blueprint_app_id() == "bp-app"


def test_layered_load_merges_global_and_agent(tmp_path, monkeypatch):
    """global.env (tenant/blueprint) + a per-agent dir .env compose into one config."""
    import entrabot.config as c

    home = tmp_path / "home"
    agentdir = tmp_path / "proj"
    monkeypatch.setenv("ENTRABOT_HOME", str(home))
    monkeypatch.setattr(c, "_entrabot_home", lambda: home)
    for k in list(_combined()):
        monkeypatch.delenv(k, raising=False)

    glob, agent = globalcfg.split(_combined())
    globalcfg.write_env(str(home / "global.env"), glob)
    globalcfg.write_env(globalcfg.agent_env_path(str(agentdir)), agent)

    c._load_dotenv()  # global base
    c.apply_agent_env(str(agentdir))  # this agent overlays
    cfg = c.get_config()
    assert cfg.tenant_id == "tid-1"  # from global
    assert cfg.blueprint_app_id == "bp-app"  # from global
    assert cfg.agent_user_upn == "bot@x.onmicrosoft.com"  # from per-agent


def test_migrate_writes_under_entrabot_home(tmp_path, monkeypatch):
    """Regression: migrate must honor $ENTRABOT_HOME for BOTH global.env and the home default
    agent .env (an earlier bug wrote the agent to the real ~/.entrabot)."""
    from entrabot.harness import cli

    home = tmp_path / "home"
    monkeypatch.setenv("ENTRABOT_HOME", str(home))
    src = tmp_path / "repo" / ".env"
    globalcfg.write_env(str(src), _combined())

    rc = cli._cmd_migrate([str(src)], set())
    assert rc == 0
    assert (home / "global.env").is_file()
    assert (home / ".env").is_file()  # default agent beside global, under ENTRABOT_HOME
    assert globalcfg.read_env(str(home / ".env"))["ENTRABOT_AGENT_USER_UPN"] == "bot@x.onmicrosoft.com"
    assert "ENTRABOT_AGENT_ID" not in globalcfg.read_env(str(home / "global.env"))


def test_agent_exists_detects_provisioned_dir(tmp_path):
    """Idempotent init keys off this: a dir whose .env carries an Agent User UPN is provisioned."""
    assert globalcfg.agent_exists(str(tmp_path)) is False
    globalcfg.write_env(globalcfg.agent_env_path(str(tmp_path)), {"ENTRABOT_AGENT_ID": "ag"})
    assert globalcfg.agent_exists(str(tmp_path)) is False  # identity row without a User UPN
    globalcfg.write_env(
        globalcfg.agent_env_path(str(tmp_path)),
        {"ENTRABOT_AGENT_ID": "ag", "ENTRABOT_AGENT_USER_UPN": "bot@x.onmicrosoft.com"},
    )
    assert globalcfg.agent_exists(str(tmp_path)) is True


def test_apply_agent_env_overrides_ambient(tmp_path, monkeypatch):
    import entrabot.config as c

    monkeypatch.setenv("ENTRABOT_AGENT_USER_UPN", "old@x.com")  # ambient (e.g. home default)
    globalcfg.write_env(
        globalcfg.agent_env_path(str(tmp_path)),
        {"ENTRABOT_AGENT_USER_UPN": "new@x.com"},
    )
    c.apply_agent_env(str(tmp_path))
    assert os.environ["ENTRABOT_AGENT_USER_UPN"] == "new@x.com"
