"""The `/users` slash command mirrors `entrabot users` from inside the running harness session.

`_handle_users` only touches `self._ui`, so we drive it with a lightweight stand-in rather than a
full session (which would need a live Copilot runtime).
"""

from types import SimpleNamespace

from entrabot.harness import globalcfg
from entrabot.harness import recipients as rc
from entrabot.harness.session import InteractiveSession

_RESOLVED_GUEST = {
    "ENTRABOT_HUMAN_USER_IDS": "g1",
    "ENTRABOT_HUMAN_UPNS": "jaly@microsoft.com",
    "ENTRABOT_HUMAN_USER_TENANT_IDS": "ms-tid",
    "ENTRABOT_HUMAN_USER_MAILS": "jaly@microsoft.com",
    "ENTRABOT_HUMAN_USER_TYPES": "Guest",
    "ENTRABOT_HUMAN_USER_ID": "g1",
    "ENTRABOT_HUMAN_UPN": "jaly@microsoft.com",
}


def _fake_session():
    lines: list[str] = []
    ui = SimpleNamespace(append_line=lambda text, style=None: lines.append(text))
    return SimpleNamespace(_ui=ui), lines


def _seed_global(monkeypatch, tmp_path):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))
    import os
    for k in list(os.environ):
        if k.startswith(globalcfg.HUMAN_PREFIX):
            monkeypatch.delenv(k, raising=False)
    globalcfg.write_env(globalcfg.global_env_path(),
                        {"ENTRABOT_TENANT_ID": "t", "ENTRABOT_BLUEPRINT_APP_ID": "bp"})


async def test_users_requires_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_HOME", str(tmp_path))  # no global
    sess, lines = _fake_session()
    await InteractiveSession._handle_users(sess, ["list"])
    assert any("entrabot init" in ln for ln in lines)


async def test_users_add_persists_and_reports(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    from entrabot.harness import setup_wizard
    monkeypatch.setattr(setup_wizard, "resolve_teams_user", lambda emails, **k: _RESOLVED_GUEST)

    sess, lines = _fake_session()
    await InteractiveSession._handle_users(sess, ["add", "jaly@microsoft.com"])
    assert [r.upn for r in rc.load_global()] == ["jaly@microsoft.com"]
    assert any("added jaly@microsoft.com" in ln for ln in lines)
    assert any("/reload" in ln for ln in lines)  # prompts to apply to live gating


async def test_users_list_and_remove(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    rc.save_global([rc.Recipient(upn="jaly@microsoft.com", user_type="Guest", tenant_id="ms"),
                    rc.Recipient(upn="bob@corp.com", user_type="Member")])

    sess, lines = _fake_session()
    await InteractiveSession._handle_users(sess, ["list"])
    assert any("jaly@microsoft.com" in ln for ln in lines)

    sess2, _ = _fake_session()
    await InteractiveSession._handle_users(sess2, ["remove", "bob@corp.com"])
    assert [r.upn for r in rc.load_global()] == ["jaly@microsoft.com"]
