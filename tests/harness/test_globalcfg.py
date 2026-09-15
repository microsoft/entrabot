import os

import pytest

from entrabot.harness.config import globalcfg


@pytest.fixture(autouse=True)
def isolate_identity_config(monkeypatch):
    from entrabot import config

    for key in (*globalcfg.AGENT_KEYS, *globalcfg.SHARED_CONFIG_KEYS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "_dotenv_candidates", lambda: [])


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
    with open(p) as handle:
        text = handle.read()
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
    assert (
        globalcfg.read_env(str(home / ".env"))["ENTRABOT_AGENT_USER_UPN"]
        == "bot@x.onmicrosoft.com"
    )
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


def test_persist_global_from_env_excludes_agent_config_and_preserves_unrelated(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    source = tmp_path / "repo" / ".env"
    globalcfg.write_env(
        globalcfg.global_env_path(),
        {
            "CUSTOM_SHARED_SETTING": "keep-existing",
            "ENTRABOT_TENANT_ID": "tid-1",
            "ENTRABOT_BLUEPRINT_APP_ID": "bp-app",
        },
    )
    globalcfg.write_env(
        str(source),
        {
            **_combined(),
            "ENTRABOT_A365_OBSERVABILITY_ENABLED": "true",
            "ENTRABOT_A365_EXPORT_ENABLED": "true",
            "NEW_SHARED_SETTING": "persist-me",
            "ENTRABOT_BLOB_CONTAINER": "primary-private-container",
        },
    )

    path = globalcfg.persist_global_from_env(str(source))

    assert path == globalcfg.global_env_path()
    saved = globalcfg.read_env(path)
    assert saved["CUSTOM_SHARED_SETTING"] == "keep-existing"
    assert "NEW_SHARED_SETTING" not in saved
    assert "ENTRABOT_BLOB_CONTAINER" not in saved
    assert saved["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] == "thumb"
    assert not any(key in saved for key in globalcfg.AGENT_KEYS)


def test_persist_global_from_env_rejects_conflicting_blueprint_without_writing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    path = globalcfg.global_env_path()
    globalcfg.write_env(
        path,
        {
            "ENTRABOT_TENANT_ID": "tid-1",
            "ENTRABOT_BLUEPRINT_APP_ID": "existing-blueprint",
            "CUSTOM_SHARED_SETTING": "unchanged",
        },
    )
    with open(path, "rb") as handle:
        before = handle.read()
    source = tmp_path / "repo" / ".env"
    globalcfg.write_env(
        str(source),
        {
            **_combined(),
            "ENTRABOT_BLUEPRINT_APP_ID": "different-blueprint",
        },
    )

    with pytest.raises(ValueError, match="ENTRABOT_BLUEPRINT_APP_ID"):
        globalcfg.persist_global_from_env(str(source))

    with open(path, "rb") as handle:
        assert handle.read() == before


def test_persist_global_from_env_replaces_same_blueprint_certificate_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    path = globalcfg.global_env_path()
    globalcfg.write_env(
        path,
        {
            "ENTRABOT_TENANT_ID": "tid-1",
            "ENTRABOT_BLUEPRINT_APP_ID": "bp-app",
            "ENTRABOT_BLUEPRINT_OBJECT_ID": "bp-obj",
            "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "old-thumb",
            "ENTRABOT_BLUEPRINT_CERT_SHA1": "old-sha1",
            "ENTRABOT_BLUEPRINT_KSP": "old-ksp",
            "CUSTOM_SHARED_SETTING": "keep",
        },
    )
    source = tmp_path / "repo" / ".env"
    rotated = {
        key: value for key, value in _combined().items() if key != "ENTRABOT_BLUEPRINT_KSP"
    }
    globalcfg.write_env(
        str(source),
        {
            **rotated,
            "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "new-thumb",
        },
    )

    globalcfg.persist_global_from_env(str(source))

    saved = globalcfg.read_env(path)
    assert saved["ENTRABOT_BLUEPRINT_CERT_THUMBPRINT"] == "new-thumb"
    assert "ENTRABOT_BLUEPRINT_CERT_SHA1" not in saved
    assert "ENTRABOT_BLUEPRINT_KSP" not in saved
    assert saved["CUSTOM_SHARED_SETTING"] == "keep"


def test_shared_certificate_resolution_does_not_mix_generations():
    resolved = globalcfg.resolve_shared_config(
        {"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "new-cert"},
        {
            "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "old-cert",
            "ENTRABOT_BLUEPRINT_CERT_SHA1": "old-sha1",
            "ENTRABOT_BLUEPRINT_KSP": "software",
        },
    )
    assert resolved == {"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "new-cert"}


def test_agent_overlay_cannot_fill_missing_ids_or_flags_from_primary(tmp_path, monkeypatch):
    from entrabot import config

    for key in globalcfg.AGENT_KEYS:
        monkeypatch.setenv(key, "true" if "ENABLED" in key else "primary-id")
    globalcfg.write_env(
        globalcfg.agent_env_path(str(tmp_path)),
        {"ENTRABOT_AGENT_ID": "second-id", "ENTRABOT_AGENT_USER_UPN": "second@example.test"},
    )

    config.apply_agent_env(str(tmp_path))

    assert os.environ["ENTRABOT_AGENT_ID"] == "second-id"
    assert config.get_config().agent_user_id is None
    assert config.get_config().agent_object_id is None
    assert not config.get_config().a365_export_enabled
    assert not config.get_config().a365_observability_enabled


def test_runtime_rejects_conflicting_agent_tenant_before_changing_identity(tmp_path, monkeypatch):
    from entrabot import config

    monkeypatch.setenv("ENTRABOT_TENANT_ID", "shared-tenant")
    monkeypatch.setenv("ENTRABOT_AGENT_ID", "primary-id")
    globalcfg.write_env(globalcfg.agent_env_path(str(tmp_path)), {
        "ENTRABOT_TENANT_ID": "conflicting-tenant", "ENTRABOT_AGENT_ID": "second-id",
    })

    with pytest.raises(ValueError, match="Conflicting ENTRABOT_TENANT_ID"):
        config.apply_agent_env(str(tmp_path))
    assert os.environ.get("ENTRABOT_AGENT_ID") == "primary-id"


def test_runtime_and_onboarding_use_same_shared_certificate_precedence(tmp_path, monkeypatch):
    from entrabot import config

    monkeypatch.setenv("ENTRABOT_BLUEPRINT_CERT_THUMBPRINT", "new-shared-cert")
    globalcfg.write_env(globalcfg.agent_env_path(str(tmp_path)), {
        "ENTRABOT_AGENT_ID": "second-id",
        "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "old-agent-cert",
        "ENTRABOT_BLUEPRINT_CERT_SHA1": "old-sha1",
    })

    config.apply_agent_env(str(tmp_path))

    actual = config.get_config()
    assert actual.blueprint_cert_thumbprint == "new-shared-cert"
    assert actual.blueprint_cert_sha1 is None


def test_runtime_dotenv_does_not_reintroduce_old_certificate_metadata(tmp_path, monkeypatch):
    from entrabot import config

    for key in globalcfg.BLUEPRINT_CERT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "_entrabot_home", lambda: tmp_path)
    clone_env = tmp_path / "clone.env"
    monkeypatch.setattr(config, "_dotenv_candidates", lambda: [clone_env])
    globalcfg.write_env(
        str(tmp_path / "global.env"), {"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "new-cert"},
    )
    globalcfg.write_env(str(clone_env), {
        "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "old-cert",
        "ENTRABOT_BLUEPRINT_CERT_SHA1": "old-sha1",
        "ENTRABOT_BLUEPRINT_KSP": "software",
    })

    config._load_dotenv()

    actual = config.get_config()
    assert actual.blueprint_cert_thumbprint == "new-cert"
    assert actual.blueprint_cert_sha1 is None
    assert actual.blueprint_ksp is None


@pytest.mark.parametrize("tenant,blueprint,new_chain", [
    ("different-tenant", "", False),
    ("tenant", "different-blueprint", False),
    ("tenant", "", True),
])
def test_setup_rejects_shared_config_conflicts_before_provisioning(
    tmp_path, monkeypatch, tenant, blueprint, new_chain,
):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    globalcfg.write_env(globalcfg.global_env_path(), {
        "ENTRABOT_TENANT_ID": "tenant", "ENTRABOT_BLUEPRINT_APP_ID": "blueprint",
    })
    with pytest.raises(ValueError, match="ENTRABOT_HOME"):
        globalcfg.validate_setup_context(
            tenant_id=tenant, blueprint_app_id=blueprint, new_chain=new_chain,
        )
    assert globalcfg.read_global()["ENTRABOT_BLUEPRINT_APP_ID"] == "blueprint"


@pytest.mark.parametrize("established", [True, False])
def test_setup_accepts_matching_or_unconfigured_shared_home(tmp_path, monkeypatch, established):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    if established:
        globalcfg.write_env(globalcfg.global_env_path(), {
            "ENTRABOT_TENANT_ID": "tenant", "ENTRABOT_BLUEPRINT_APP_ID": "blueprint",
        })
    globalcfg.validate_setup_context(
        tenant_id="tenant", blueprint_app_id="blueprint", new_chain=not established,
    )


def test_agent_composition_has_one_identity_and_certificate_source():
    base = {
        **_combined(),
        "ENTRABOT_A365_EXPORT_ENABLED": "true",
        "ENTRABOT_BLUEPRINT_CERT_SHA1": "new-sha1",
        "CUSTOM_SETTING": "shared",
    }
    agent = {
        "ENTRABOT_AGENT_ID": "second",
        "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT": "stale-cert",
        "ENTRABOT_BLUEPRINT_CERT_SHA1": "stale-sha1",
        "CUSTOM_SETTING": "selected",
    }

    composed = globalcfg.resolve_agent_env(base, agent=agent)

    assert composed["ENTRABOT_AGENT_ID"] == "second"
    assert "ENTRABOT_AGENT_USER_ID" not in composed
    assert "ENTRABOT_A365_EXPORT_ENABLED" not in composed
    assert composed["ENTRABOT_BLUEPRINT_CERT_SHA1"] == "new-sha1"
    assert composed["CUSTOM_SETTING"] == "selected"
    assert base["ENTRABOT_A365_EXPORT_ENABLED"] == "true"
    assert agent["ENTRABOT_BLUEPRINT_CERT_SHA1"] == "stale-sha1"


def test_shared_persistence_does_not_rewrite_unchanged_config(tmp_path, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path / "home"))
    source = str(tmp_path / ".env")
    globalcfg.write_env(source, _combined())
    path = globalcfg.persist_global_from_env(source)
    writer = Mock(side_effect=AssertionError("unchanged shared configuration was rewritten"))
    monkeypatch.setattr(globalcfg, "write_env", writer)

    assert globalcfg.persist_global_from_env(source) == path
    writer.assert_not_called()
