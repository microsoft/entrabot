"""Tests for `entrabot users` — read-only listing of the agent's Entra sponsors.
(Sponsor add/remove is done in Entra directly, not in the harness.)"""

from types import SimpleNamespace

from entrabot.harness import cli
from entrabot.harness.config import globalcfg


def _seed_global(monkeypatch, tmp_path):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    globalcfg.write_env(globalcfg.global_env_path(),
                        {"ENTRABOT_TENANT_ID": "t", "ENTRABOT_BLUEPRINT_APP_ID": "bp"})


def _mock_fetch(monkeypatch, emails):
    """Patch the core read function — raises (no sponsors) when empty, else returns records."""
    from entrabot.identity import sponsors as cs

    def fetch(cfg, **k):
        if not emails:
            raise ValueError("Agent Identity has no user sponsors")
        return [SimpleNamespace(mail=e, user_principal_name=e, user_id="id-" + e) for e in emails]

    monkeypatch.setattr(cs, "fetch_agent_identity_sponsors", fetch)


def test_users_requires_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))  # no global
    assert cli._cmd_users([], set()) == 1
    assert "init" in capsys.readouterr().out.lower()


def test_users_list_empty(tmp_path, monkeypatch, capsys):
    _seed_global(monkeypatch, tmp_path)
    _mock_fetch(monkeypatch, [])
    assert cli._cmd_users([], set()) == 0
    assert "No sponsors" in capsys.readouterr().out


def test_users_list_shows_sponsors(tmp_path, monkeypatch, capsys):
    _seed_global(monkeypatch, tmp_path)
    _mock_fetch(monkeypatch, ["jaly@microsoft.com", "bob@corp.com"])
    assert cli._cmd_users([], set()) == 0
    out = capsys.readouterr().out
    assert "jaly@microsoft.com" in out and "bob@corp.com" in out
