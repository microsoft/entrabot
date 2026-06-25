"""Guards for the watched-chat member fetch used by the sponsor gate.

``fetch_watched_chat_members_by_id`` must read members for every watched chat
while reusing a single Agent User token + a single ``httpx.Client`` — fanning
out one token acquisition + one client per chat is a performance/reliability
regression (PR #88 review).
"""

from __future__ import annotations

from typing import Any

import httpx

from entrabot.config import EntraBotConfig
from entrabot.identity.sponsors import (
    fetch_chat_members,
    fetch_watched_chat_members_by_id,
)


def _empty_members_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    return httpx.MockTransport(handler)


def _counting_token_provider() -> tuple[Any, dict]:
    calls = {"n": 0}

    def provider(_config: EntraBotConfig) -> str:
        calls["n"] += 1
        return "fake-token"

    return provider, calls


def test_fetch_watched_members_by_id_acquires_token_once(tmp_path) -> None:
    (tmp_path / "watched_chats").write_text(
        "19:a_b@unq.gbl.spaces\n19:c_d@unq.gbl.spaces\n19:e_f@unq.gbl.spaces\n"
    )
    config = EntraBotConfig(agent_user_id="agent", data_dir=tmp_path)
    provider, calls = _counting_token_provider()

    result = fetch_watched_chat_members_by_id(
        config, token_provider=provider, transport=_empty_members_transport()
    )

    # One token acquisition for N chats, and every chat keyed in the result.
    assert calls["n"] == 1
    assert set(result.keys()) == {
        "19:a_b@unq.gbl.spaces",
        "19:c_d@unq.gbl.spaces",
        "19:e_f@unq.gbl.spaces",
    }


def test_fetch_watched_members_by_id_empty_when_no_watched_file(tmp_path) -> None:
    config = EntraBotConfig(agent_user_id="agent", data_dir=tmp_path)
    provider, calls = _counting_token_provider()

    result = fetch_watched_chat_members_by_id(
        config, token_provider=provider, transport=_empty_members_transport()
    )

    assert result == {}
    # No chats → no token acquisition.
    assert calls["n"] == 0


def test_fetch_chat_members_flat_still_works(tmp_path) -> None:
    """The flat helper (used by share_file) keeps returning a flat member list
    and reuses a single token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [{"userId": "u1", "displayName": "U1", "email": "u1@x", "roles": []}]},
        )

    config = EntraBotConfig(agent_user_id="agent", data_dir=tmp_path)
    provider, calls = _counting_token_provider()

    members = fetch_chat_members(
        config,
        ["19:a_b@unq.gbl.spaces", "19:c_d@unq.gbl.spaces"],
        token_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    assert [m["user_id"] for m in members] == ["u1", "u1"]
    assert calls["n"] == 1
