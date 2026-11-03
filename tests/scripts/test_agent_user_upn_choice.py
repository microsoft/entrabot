"""Which Agent User a run selects when .env still names the one from an earlier run.

Setup writes the UPN to .env, and importing entrabot.config loads it into the environment. A
later run that asks for a different suffix must get that agent, not the cached one.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from tests.scripts._blueprint_cert_helpers import REPO_ROOT


@pytest.fixture
def provisioning(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    module = importlib.import_module("create_entra_agent_ids")
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"value": [{"id": "contoso.com", "isVerified": True, "isDefault": True}]},
        text="",
    )
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.delenv("ENTRABOT_AGENT_USER_UPN", raising=False)
    monkeypatch.delenv("_ENTRABOT_UPN_SUFFIX", raising=False)
    return module


def test_a_requested_suffix_beats_a_upn_cached_in_env(
    provisioning: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENTRABOT_AGENT_USER_UPN", "entrabot-agent-a@contoso.com")
    monkeypatch.setenv("_ENTRABOT_UPN_SUFFIX", "b")

    assert provisioning._agent_user_upn("token") == "entrabot-agent-b@contoso.com"


def test_an_explicit_upn_is_used_when_no_suffix_was_asked_for(
    provisioning: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENTRABOT_AGENT_USER_UPN", "chosen@contoso.com")

    assert provisioning._agent_user_upn("token") == "chosen@contoso.com"


def test_with_neither_the_default_name_is_used(provisioning: object) -> None:
    assert provisioning._agent_user_upn("token") == "entrabot-agent@contoso.com"
