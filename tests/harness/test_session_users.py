"""The `/users` slash command — sponsor management over the Entra Agent-Identity relationship.

`_handle_users` / `_set_sponsor` only touch `self._ui` + core identity.sponsors (mocked), so we
drive them with a lightweight stand-in rather than a full session.
"""

from types import SimpleNamespace

from entrabot.harness.session import InteractiveSession


def _fake_session():
    lines: list[str] = []
    ui = SimpleNamespace(append_line=lambda text, style=None: lines.append(text))
    sess = SimpleNamespace(_ui=ui, _sponsors=set())
    sess._load_sponsors = lambda: InteractiveSession._load_sponsors(sess)
    sess._sponsor_records = lambda: InteractiveSession._sponsor_records(sess)

    async def _refresh():
        await InteractiveSession._refresh_sponsors(sess)

    async def _set_sponsor(email, *, sponsor):
        await InteractiveSession._set_sponsor(sess, email, sponsor=sponsor)

    sess._refresh_sponsors = _refresh
    sess._set_sponsor = _set_sponsor
    return sess, lines


def _mock_core(monkeypatch, state):
    """Patch core identity.sponsors. state['ids'] is the live Agent-ID sponsor set."""
    from entrabot.identity import sponsors as cs

    def add(cfg, email, **k):
        state["ids"].add(email)
        return ("id-" + email, email)

    def remove(cfg, email, **k):
        existed = email in state["ids"]
        state["ids"].discard(email)
        return (email, existed)

    def gate(cfg):
        return SimpleNamespace(user_ids=frozenset("id-" + e for e in state["ids"]))

    def lst(cfg, **k):
        return [SimpleNamespace(mail=e, user_principal_name=e, user_id="id-" + e)
                for e in state["ids"]]

    monkeypatch.setattr(cs, "add_sponsor_by_email", add)
    monkeypatch.setattr(cs, "remove_sponsor_by_email", remove)
    monkeypatch.setattr(cs, "load_agent_identity_sponsor_gate", gate)
    monkeypatch.setattr(cs, "list_agent_identity_sponsors", lst)


async def test_users_list_empty(monkeypatch):
    _mock_core(monkeypatch, {"ids": set()})
    sess, lines = _fake_session()
    await InteractiveSession._handle_users(sess, ["list"])
    assert any("no sponsors" in ln.lower() for ln in lines)


async def test_users_sponsor_then_guest(monkeypatch):
    state = {"ids": set()}
    _mock_core(monkeypatch, state)
    sess, _ = _fake_session()

    await InteractiveSession._handle_users(sess, ["sponsor", "jaly@microsoft.com"])
    assert state["ids"] == {"jaly@microsoft.com"}  # wrote the Entra relationship
    assert sess._sponsors == {"id-jaly@microsoft.com"}  # refreshed live from the gate

    await InteractiveSession._handle_users(sess, ["guest", "jaly@microsoft.com"])
    assert state["ids"] == set()
    assert sess._sponsors == set()


async def test_users_list_shows_sponsors(monkeypatch):
    _mock_core(monkeypatch, {"ids": {"jaly@microsoft.com"}})
    sess, lines = _fake_session()
    await InteractiveSession._handle_users(sess, ["list"])
    assert any("jaly@microsoft.com" in ln for ln in lines)


async def test_users_guest_unknown_warns(monkeypatch):
    _mock_core(monkeypatch, {"ids": set()})
    sess, lines = _fake_session()
    await InteractiveSession._handle_users(sess, ["guest", "ghost@x.com"])
    assert any("was not a sponsor" in ln for ln in lines)


def test_load_sponsors_reads_core_gate(monkeypatch):
    from entrabot.identity import sponsors as cs
    monkeypatch.setattr(cs, "load_agent_identity_sponsor_gate",
                        lambda cfg: SimpleNamespace(user_ids=frozenset({"g1"})))
    s = SimpleNamespace()
    assert InteractiveSession._load_sponsors(s) == {"g1"}  # straight from the Entra gate
