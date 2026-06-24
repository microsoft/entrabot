"""Write side of the Agent Identity sponsor API: add/remove a user sponsor on the service
principal. Mirrors scripts/add_agent_sponsor.py's Graph calls but config-driven (config
.agent_object_id) using the Agent Identity FIC token. Graph is mocked via httpx.MockTransport."""

import httpx
import pytest

from entrabot.config import EntraBotConfig
from entrabot.errors import GraphApiError, TokenExpiredError
from entrabot.identity import sponsors


def _cfg():
    return EntraBotConfig(agent_object_id="agent-obj-1")


def _tok(_cfg):
    return "fic-token"


def test_add_sponsor_posts_ref_and_returns_true():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["body"] = req.read().decode()
        return httpx.Response(204)

    ok = sponsors.add_agent_identity_sponsor(
        _cfg(), "user-9", token_provider=_tok, transport=httpx.MockTransport(handler))
    assert ok is True
    assert seen["method"] == "POST"
    assert seen["url"].endswith(
        "/servicePrincipals/agent-obj-1/microsoft.graph.agentIdentity/sponsors/$ref")
    assert "/users/user-9" in seen["body"]  # @odata.id points at the user


def test_add_sponsor_already_exists_returns_false():
    def handler(req):
        return httpx.Response(
            400, json={"error": {"message": "One or more added object references already exist"}})

    ok = sponsors.add_agent_identity_sponsor(
        _cfg(), "user-9", token_provider=_tok, transport=httpx.MockTransport(handler))
    assert ok is False  # idempotent, not an error


def test_add_sponsor_401_raises_token_expired():
    def handler(req):
        return httpx.Response(401, json={"error": "expired"})

    with pytest.raises(TokenExpiredError):
        sponsors.add_agent_identity_sponsor(
            _cfg(), "user-9", token_provider=_tok, transport=httpx.MockTransport(handler))


def test_add_sponsor_other_error_raises_graph_error():
    def handler(req):
        return httpx.Response(403, text="Forbidden")

    with pytest.raises(GraphApiError):
        sponsors.add_agent_identity_sponsor(
            _cfg(), "user-9", token_provider=_tok, transport=httpx.MockTransport(handler))


def test_add_sponsor_requires_agent_object_id():
    with pytest.raises(ValueError):
        sponsors.add_agent_identity_sponsor(EntraBotConfig(), "user-9", token_provider=_tok)


def test_remove_sponsor_deletes_ref_and_returns_true():
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["url"] = str(req.url)
        return httpx.Response(204)

    removed = sponsors.remove_agent_identity_sponsor(
        _cfg(), "user-9", token_provider=_tok, transport=httpx.MockTransport(handler))
    assert removed is True
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith(
        "/microsoft.graph.agentIdentity/sponsors/user-9/$ref")


def test_remove_sponsor_404_returns_false():
    def handler(req):
        return httpx.Response(404, json={"error": "not found"})

    removed = sponsors.remove_agent_identity_sponsor(
        _cfg(), "user-9", token_provider=_tok, transport=httpx.MockTransport(handler))
    assert removed is False


# ── email-based convenience (resolve → write), resolution injected ─────────────
def test_add_sponsor_by_email_resolves_then_adds():
    calls = {}

    def fake_resolve(token, email):
        calls["resolve"] = (token, email)
        return ("resolved-id", "Alice")

    def handler(req):
        calls["body"] = req.read().decode()
        return httpx.Response(204)

    user_id, name = sponsors.add_sponsor_by_email(
        _cfg(), "alice@example.com",
        resolve=fake_resolve, user_token_provider=lambda _c: "user-token",
        token_provider=_tok, transport=httpx.MockTransport(handler))
    assert (user_id, name) == ("resolved-id", "Alice")
    assert calls["resolve"] == ("user-token", "alice@example.com")  # User.Read.All token
    assert "/users/resolved-id" in calls["body"]  # the resolved id was written


def test_remove_sponsor_by_email_resolves_then_removes():
    def fake_resolve(token, email):
        return ("resolved-id", "Alice")

    def handler(req):
        assert req.method == "DELETE"
        return httpx.Response(204)

    name, removed = sponsors.remove_sponsor_by_email(
        _cfg(), "alice@example.com",
        resolve=fake_resolve, user_token_provider=lambda _c: "user-token",
        token_provider=_tok, transport=httpx.MockTransport(handler))
    assert name == "Alice" and removed is True
