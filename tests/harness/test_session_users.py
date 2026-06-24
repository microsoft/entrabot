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
    sess = SimpleNamespace(_ui=ui, _sponsors=set())
    # bind the real sponsor methods to this fake self so /users dispatch + matrix work
    sess._load_sponsors = lambda: InteractiveSession._load_sponsors(sess)

    async def _refresh():
        await InteractiveSession._refresh_sponsors(sess)

    async def _set_sponsor(email, *, sponsor):
        await InteractiveSession._set_sponsor(sess, email, sponsor=sponsor)

    sess._refresh_sponsors = _refresh
    sess._set_sponsor = _set_sponsor
    return sess, lines


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
    # adding a recipient is the talk-to list; sponsor authority is a separate step
    assert any("sponsor" in ln.lower() for ln in lines)


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


def _mock_core_sponsors(monkeypatch, state):
    """Patch core identity.sponsors so /users sponsor management hits no network. `state["ids"]`
    is the live Agent-ID sponsor set the gate reports; add/remove mutate it."""
    from entrabot.identity import sponsors as cs

    def add(cfg, email, **k):
        state["ids"].add("g1")
        return ("g1", "Jaly")

    def remove(cfg, email, **k):
        existed = "g1" in state["ids"]
        state["ids"].discard("g1")
        return ("Jaly", existed)

    monkeypatch.setattr(cs, "add_sponsor_by_email", add)
    monkeypatch.setattr(cs, "remove_sponsor_by_email", remove)
    monkeypatch.setattr(cs, "load_agent_identity_sponsor_gate",
                        lambda cfg: SimpleNamespace(user_ids=frozenset(state["ids"])))


async def test_users_sponsor_guest_shortcuts(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    rc.save_global([rc.Recipient(upn="jaly@microsoft.com", user_id="g1", user_type="Guest")])
    state = {"ids": set()}
    _mock_core_sponsors(monkeypatch, state)

    sess, _ = _fake_session()
    await InteractiveSession._handle_users(sess, ["sponsor", "jaly@microsoft.com"])
    assert sess._sponsors == {"g1"}  # wrote to Entra, refreshed live from the gate
    await InteractiveSession._handle_users(sess, ["guest", "jaly@microsoft.com"])
    assert sess._sponsors == set()


async def test_users_matrix_writes_entra_sponsors(tmp_path, monkeypatch):
    _seed_global(monkeypatch, tmp_path)
    rc.save_global([rc.Recipient(upn="jaly@microsoft.com", user_id="g1", user_type="Guest"),
                    rc.Recipient(upn="bob@corp.com", user_id="m1", user_type="Member")])
    state = {"ids": set()}
    _mock_core_sponsors(monkeypatch, state)

    seen_rows = {}

    async def fake_edit_users(rows):
        seen_rows["rows"] = rows
        return {"roles": {"jaly@microsoft.com": True, "bob@corp.com": False}}

    sess, _ = _fake_session()
    sess._ui.edit_users = fake_edit_users
    await InteractiveSession._users_matrix(sess, rc)

    # the matrix gets recipients with Type + Role (read from the gate; none are sponsors yet)
    assert {r["upn"] for r in seen_rows["rows"]} == {"jaly@microsoft.com", "bob@corp.com"}
    assert all(r["role"] is False for r in seen_rows["rows"])
    # toggling jaly on wrote the Entra relationship and refreshed the live set
    assert sess._sponsors == {"g1"}


def test_load_sponsors_reads_core_gate(monkeypatch):
    from entrabot.identity import sponsors as cs
    monkeypatch.setattr(cs, "load_agent_identity_sponsor_gate",
                        lambda cfg: SimpleNamespace(user_ids=frozenset({"g1"})))
    s = SimpleNamespace()
    assert InteractiveSession._load_sponsors(s) == {"g1"}  # straight from the Entra gate
