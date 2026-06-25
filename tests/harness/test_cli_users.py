"""Tests for `entrabot users` — sponsor management (list / sponsor / guest) over the Entra
Agent-Identity relationship. Core identity.sponsors is mocked so no Graph/token is needed."""

from types import SimpleNamespace

from entrabot.harness import cli, globalcfg


def _seed_global(monkeypatch, tmp_path):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    globalcfg.write_env(globalcfg.global_env_path(),
                        {"ENTRABOT_TENANT_ID": "t", "ENTRABOT_BLUEPRINT_APP_ID": "bp"})


def _mock_core(monkeypatch, state):
    from entrabot.identity import sponsors as cs

    def add(cfg, email, **k):
        state["ids"].append(email)
        return ("id-" + email, email)

    def remove(cfg, email, **k):
        existed = email in state["ids"]
        if existed:
            state["ids"].remove(email)
        return (email, existed)

    def lst(cfg, **k):
        return [SimpleNamespace(mail=e, user_principal_name=e, user_id=e) for e in state["ids"]]

    monkeypatch.setattr(cs, "add_sponsor_by_email", add)
    monkeypatch.setattr(cs, "remove_sponsor_by_email", remove)
    monkeypatch.setattr(cs, "list_agent_identity_sponsors", lst)


def test_users_requires_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))  # no global
    assert cli._cmd_users(["list"], set()) == 1
    assert "init" in capsys.readouterr().out.lower()


def test_users_list_empty(tmp_path, monkeypatch, capsys):
    _seed_global(monkeypatch, tmp_path)
    _mock_core(monkeypatch, {"ids": []})
    assert cli._cmd_users(["list"], set()) == 0
    assert "No sponsors" in capsys.readouterr().out


def test_users_add_then_list(tmp_path, monkeypatch, capsys):
    _seed_global(monkeypatch, tmp_path)
    state = {"ids": []}
    _mock_core(monkeypatch, state)

    assert cli._cmd_users(["sponsor", "jaly@microsoft.com"], set()) == 0
    assert state["ids"] == ["jaly@microsoft.com"]  # wrote the Entra sponsor relationship

    capsys.readouterr()
    assert cli._cmd_users(["list"], set()) == 0
    assert "jaly@microsoft.com" in capsys.readouterr().out  # list reads the gate


def test_users_remove(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    state = {"ids": ["jaly@microsoft.com"]}
    _mock_core(monkeypatch, state)
    assert cli._cmd_users(["guest", "jaly@microsoft.com"], set()) == 0
    assert state["ids"] == []


def test_users_remove_unknown_is_nonzero(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    _mock_core(monkeypatch, {"ids": []})
    assert cli._cmd_users(["guest", "ghost@x.com"], set()) == 1  # wasn't a sponsor


def test_users_add_unknown_is_nonzero(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    from entrabot.identity import sponsors as cs

    def add(cfg, email, **k):
        raise LookupError(email)

    monkeypatch.setattr(cs, "add_sponsor_by_email", add)
    assert cli._cmd_users(["sponsor", "ghost@x.com"], set()) == 1
