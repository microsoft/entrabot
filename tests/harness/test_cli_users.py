"""Tests for the `entrabot users` management subcommand (list / add / remove)."""

from entrabot.harness import cli, globalcfg
from entrabot.harness import recipients as rc

_RESOLVED_GUEST = {
    "ENTRABOT_HUMAN_USER_IDS": "g1",
    "ENTRABOT_HUMAN_UPNS": "jaly@microsoft.com",
    "ENTRABOT_HUMAN_USER_TENANT_IDS": "ms-tid",
    "ENTRABOT_HUMAN_USER_MAILS": "jaly@microsoft.com",
    "ENTRABOT_HUMAN_USER_TYPES": "Guest",
    "ENTRABOT_HUMAN_USER_ID": "g1",
    "ENTRABOT_HUMAN_UPN": "jaly@microsoft.com",
}


def _seed_global(monkeypatch, tmp_path):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    import os
    for k in list(os.environ):
        if k.startswith(globalcfg.HUMAN_PREFIX):
            monkeypatch.delenv(k, raising=False)
    globalcfg.write_env(globalcfg.global_env_path(),
                        {"ENTRABOT_TENANT_ID": "t", "ENTRABOT_BLUEPRINT_APP_ID": "bp"})


def test_users_requires_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))  # empty home → no global
    assert cli._cmd_users(["list"], set()) == 1
    assert "init" in capsys.readouterr().out.lower()


def test_users_add_then_list(tmp_path, monkeypatch, capsys):
    _seed_global(monkeypatch, tmp_path)
    from entrabot.harness import setup_wizard
    monkeypatch.setattr(setup_wizard, "resolve_teams_user", lambda emails, **k: _RESOLVED_GUEST)

    assert cli._cmd_users(["add", "jaly@microsoft.com"], set()) == 0
    assert [r.upn for r in rc.load_global()] == ["jaly@microsoft.com"]
    # tenant/blueprint preserved through the edit
    assert globalcfg.read_env(globalcfg.global_env_path())["ENTRABOT_TENANT_ID"] == "t"

    capsys.readouterr()  # clear
    assert cli._cmd_users(["list"], set()) == 0
    out = capsys.readouterr().out
    assert "jaly@microsoft.com" in out
    assert "Guest" in out


def test_users_add_is_additive(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    rc.save_global([rc.Recipient(upn="bob@corp.com", user_type="Member")])
    from entrabot.harness import setup_wizard
    monkeypatch.setattr(setup_wizard, "resolve_teams_user", lambda emails, **k: _RESOLVED_GUEST)

    assert cli._cmd_users(["add", "jaly@microsoft.com"], set()) == 0
    upns = sorted(r.upn for r in rc.load_global())
    assert upns == ["bob@corp.com", "jaly@microsoft.com"]  # bob not clobbered


def test_users_remove(tmp_path, monkeypatch, capsys):
    _seed_global(monkeypatch, tmp_path)
    rc.save_global([
        rc.Recipient(upn="jaly@microsoft.com", user_id="g1", tenant_id="ms", user_type="Guest"),
        rc.Recipient(upn="bob@corp.com", user_type="Member"),
    ])
    assert cli._cmd_users(["remove", "bob@corp.com"], set()) == 0
    assert [r.upn for r in rc.load_global()] == ["jaly@microsoft.com"]


def test_users_remove_unknown_is_nonzero(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    rc.save_global([rc.Recipient(upn="jaly@microsoft.com")])
    assert cli._cmd_users(["remove", "ghost@x.com"], set()) == 1


def test_users_elevate_and_demote(tmp_path, monkeypatch, capsys):
    _seed_global(monkeypatch, tmp_path)
    rc.save_global([rc.Recipient(upn="jaly@microsoft.com", user_id="g1", user_type="Guest")])

    assert cli._cmd_users(["sponsor", "jaly@microsoft.com"], set()) == 0
    assert rc.load_global()[0].sponsor is True

    capsys.readouterr()
    assert cli._cmd_users(["list"], set()) == 0
    assert "Sponsor" in capsys.readouterr().out

    assert cli._cmd_users(["guest", "jaly@microsoft.com"], set()) == 0
    assert rc.load_global()[0].sponsor is False


def test_users_elevate_unknown_is_nonzero(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    rc.save_global([rc.Recipient(upn="jaly@microsoft.com")])
    assert cli._cmd_users(["sponsor", "ghost@x.com"], set()) == 1
