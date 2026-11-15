"""The `/users` slash command — read-only listing of the agent's Entra sponsors.

`_handle_users` only touches `self._ui` + the core read functions (mocked), so we drive it with a
lightweight stand-in rather than a full session.
"""

from types import SimpleNamespace

from entrabot.harness.session import InteractiveSession


def _fake_session():
    lines: list[str] = []
    ui = SimpleNamespace(append_line=lambda text, style=None: lines.append(text))
    sess = SimpleNamespace(_ui=ui)
    sess._sponsor_records = lambda: InteractiveSession._sponsor_records(sess)
    return sess, lines


def _mock_fetch(monkeypatch, emails):
    from entrabot.identity import sponsors as cs

    def fetch(cfg, **k):
        if not emails:
            raise ValueError("Agent Identity has no user sponsors")
        return [SimpleNamespace(mail=e, user_principal_name=e, user_id="id-" + e) for e in emails]

    monkeypatch.setattr(cs, "fetch_agent_identity_sponsors", fetch)


async def test_users_list_empty(monkeypatch):
    _mock_fetch(monkeypatch, [])
    sess, lines = _fake_session()
    await InteractiveSession._handle_users(sess, [])
    assert any("no sponsors" in ln.lower() for ln in lines)


async def test_users_list_shows_sponsors(monkeypatch):
    _mock_fetch(monkeypatch, ["jaly@microsoft.com"])
    sess, lines = _fake_session()
    await InteractiveSession._handle_users(sess, [])
    assert any("jaly@microsoft.com" in ln for ln in lines)


def test_load_sponsors_reads_core_gate(monkeypatch):
    from entrabot.identity import sponsors as cs
    monkeypatch.setattr(cs, "load_agent_identity_sponsor_gate",
                        lambda cfg: SimpleNamespace(user_ids=frozenset({"g1"})))
    s = SimpleNamespace()
    assert InteractiveSession._load_sponsors(s) == {"g1"}  # caller-class gate, read-only
