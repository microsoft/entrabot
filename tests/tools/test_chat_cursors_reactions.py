"""The reaction ledger in the per-chat cursor.

It is stored beside ``last_ts`` and ``seen_ids_tail`` but is separate state: the message
ledger never changes it, and it never changes the message ledger.
"""

from __future__ import annotations

import json

import pytest

import entrabot.tools.chat_cursors as cc
from entrabot.storage.blob import ConcurrencyError
from entrabot.tools.chat_cursors import (
    REACTION_KEYS_FIELD,
    claim_delivery,
    claim_reactions,
    cursor_key,
    load_cursor,
    save_cursor,
)
from entrabot.tools.reactions import (
    MAX_REACTION_KEYS,
    MessageReaction,
    ReactionKey,
    ReactionObservation,
)

CHAT = "19:c@thread.v2"
ALICE = "11111111-0000-4000-8000-111111111111"
BOB = "22222222-0000-4000-8000-222222222222"
THUMBS_UP = "\U0001f44d"
BASE_STATE = {
    "last_ts": "2026-11-17T09:00:00Z",
    "seen_ids_tail": ["m1"],
    "bootstrapped": True,
}


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
    return tmp_path


def _key(message_id: str = "100", user_id: str = ALICE, reaction: str = THUMBS_UP) -> ReactionKey:
    return ReactionKey(message_id, user_id, reaction)


def _observation(*keys: ReactionKey, messages: tuple[str, ...] = ("100",)) -> ReactionObservation:
    return ReactionObservation(
        message_ids=frozenset(messages),
        reactions={
            key: MessageReaction(key.reaction_type, "", key.user_id, "2026-11-17T10:00:00Z")
            for key in keys
        },
    )


def _stored_keys(chat_id: str = CHAT) -> list[list[str]] | None:
    cursor = load_cursor(chat_id)
    assert cursor is not None
    return cursor.get(REACTION_KEYS_FIELD)


class TestSaveCursorKeepsTheLedger:
    def test_a_ledger_in_the_state_is_written(self, tmp_data_dir) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: [["100", ALICE, THUMBS_UP]]})
        assert _stored_keys() == [["100", ALICE, THUMBS_UP]]

    def test_no_ledger_in_the_state_writes_none(self, tmp_data_dir) -> None:
        """Absent, not empty: an empty list would say the state had been built."""
        save_cursor(CHAT, BASE_STATE)
        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert REACTION_KEYS_FIELD not in cursor

    def test_the_written_ledger_is_bounded(self, tmp_data_dir) -> None:
        many = [[str(1000 + n), ALICE, "like"] for n in range(MAX_REACTION_KEYS + 40)]
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: many})
        stored = _stored_keys()
        assert stored is not None
        assert len(stored) == MAX_REACTION_KEYS
        assert [str(1000 + MAX_REACTION_KEYS + 39), ALICE, "like"] in stored


class TestClaimDeliveryLeavesTheLedgerAlone:
    def test_a_message_claim_keeps_the_stored_ledger(self, tmp_data_dir) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: [["100", ALICE, THUMBS_UP]]})

        assert claim_delivery(CHAT, ["m2"], "2026-11-17T09:05:00Z") == ["m2"]

        assert _stored_keys() == [["100", ALICE, THUMBS_UP]]

    def test_a_message_claim_does_not_invent_a_ledger(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)
        claim_delivery(CHAT, ["m2"], "2026-11-17T09:05:00Z")
        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert REACTION_KEYS_FIELD not in cursor


class TestClaimReactions:
    def test_the_first_build_seeds_silently(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)

        claim = claim_reactions(CHAT, _observation(_key()), known=None, base_state=BASE_STATE)

        assert claim.resolved is True
        assert claim.added == ()
        assert claim.ledger == frozenset({_key()})
        assert _stored_keys() == [["100", ALICE, THUMBS_UP]]

    def test_an_empty_first_build_is_stored_as_empty_not_missing(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)
        claim_reactions(CHAT, _observation(), known=None, base_state=BASE_STATE)
        assert _stored_keys() == []

    def test_a_reaction_added_after_the_seed_is_claimed_once(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)
        claim_reactions(CHAT, _observation(), known=None, base_state=BASE_STATE)

        first = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)
        again = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)

        assert first.added == (_key(),)
        assert again.resolved is True
        assert again.added == ()
        assert again.ledger == frozenset({_key()})

    def test_the_message_cursor_is_left_exactly_as_it_was(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)

        claim_reactions(CHAT, _observation(_key()), known=None, base_state={})

        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert cursor["last_ts"] == BASE_STATE["last_ts"]
        assert cursor["seen_ids_tail"] == ["m1"]
        assert cursor["bootstrapped"] is True

    def test_a_removed_reaction_leaves_the_stored_ledger(self, tmp_data_dir) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: [["100", ALICE, THUMBS_UP]]})

        gone = claim_reactions(CHAT, _observation(), known={_key()}, base_state=BASE_STATE)
        assert gone.added == ()
        assert _stored_keys() == []

        back = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)
        assert back.added == (_key(),)

    def test_nothing_is_written_when_nothing_changed(self, tmp_data_dir, monkeypatch) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: [["100", ALICE, THUMBS_UP]]})
        before = load_cursor(CHAT)
        backend = cc.get_backend()
        writes: list[str] = []
        real_write = backend.write_text

        def counting_write(key, content, **kwargs):
            writes.append(key)
            return real_write(key, content, **kwargs)

        monkeypatch.setattr(backend, "write_text", counting_write)
        monkeypatch.setattr(cc, "get_backend", lambda: backend)

        claim = claim_reactions(CHAT, _observation(_key()), known=None, base_state=BASE_STATE)

        assert claim.resolved is True and claim.added == ()
        assert writes == []
        assert load_cursor(CHAT) == before

    def test_a_ledger_this_process_built_is_the_baseline_when_storage_lost_it(
        self, tmp_data_dir
    ) -> None:
        """An older instance in the fleet rewrites the cursor without the ledger. What this
        process already knows still tells a new reaction from an old one."""
        save_cursor(CHAT, BASE_STATE)  # no ledger stored

        claim = claim_reactions(
            CHAT,
            _observation(_key(), _key(user_id=BOB)),
            known={_key()},
            base_state=BASE_STATE,
        )

        assert claim.added == (_key(user_id=BOB),)
        assert _stored_keys() == [["100", ALICE, THUMBS_UP], ["100", BOB, THUMBS_UP]]

    def test_two_instances_one_push(self, tmp_data_dir, monkeypatch) -> None:
        from entrabot.storage.backend import BlobBackend
        from tests.storage.test_backend import _FakeBlobStore

        shared = _FakeBlobStore()
        monkeypatch.setattr(cc, "get_backend", lambda: BlobBackend(shared))
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: []})

        a = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)
        b = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)

        assert [*a.added, *b.added] == [_key()]
        assert b.resolved is True and b.ledger == frozenset({_key()})

    def test_a_missing_cursor_is_created_from_the_in_memory_state(self, tmp_data_dir) -> None:
        claim = claim_reactions(CHAT, _observation(_key()), known=None, base_state=BASE_STATE)

        assert claim.resolved is True and claim.added == ()
        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert cursor["last_ts"] == BASE_STATE["last_ts"]
        assert cursor["seen_ids_tail"] == ["m1"]
        assert cursor["bootstrapped"] is True
        assert cursor[REACTION_KEYS_FIELD] == [["100", ALICE, THUMBS_UP]]

    def test_a_missing_cursor_is_never_created_without_a_watermark(self, tmp_data_dir) -> None:
        """A cursor with no ``last_ts`` would make the next restart replay the chat."""
        claim = claim_reactions(
            CHAT, _observation(_key()), known=None, base_state={"bootstrapped": True}
        )

        assert claim.resolved is False
        assert claim.added == ()
        assert load_cursor(CHAT) is None

    def test_a_read_failure_fails_closed(self, tmp_data_dir, monkeypatch) -> None:
        class ExplodingBackend:
            def read_text_with_etag(self, key: str):
                raise OSError("transient read failure")

        monkeypatch.setattr(cc, "get_backend", lambda: ExplodingBackend())

        claim = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)

        assert claim.resolved is False
        assert claim.added == ()

    @pytest.mark.parametrize("payload", ["{not json", "[1, 2, 3]"])
    def test_a_corrupt_cursor_fails_closed_and_is_not_overwritten(
        self, tmp_data_dir, payload: str
    ) -> None:
        cc.get_backend().write_text(cursor_key(CHAT), payload)

        claim = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)

        assert claim.resolved is False
        assert claim.added == ()
        assert cc.get_backend().read_text(cursor_key(CHAT)) == payload

    def test_a_write_failure_fails_closed(self, tmp_data_dir, monkeypatch) -> None:
        class ReadOnlyBackend:
            def read_text_with_etag(self, key: str):
                return json.dumps({**BASE_STATE, REACTION_KEYS_FIELD: []}), "etag-1"

            def write_text(self, key, content, **kwargs):
                raise OSError("disk full")

        monkeypatch.setattr(cc, "get_backend", lambda: ReadOnlyBackend())

        claim = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)

        assert claim.resolved is False
        assert claim.added == ()

    def test_a_lost_race_re_reads_and_claims_only_what_is_still_new(
        self, tmp_data_dir, monkeypatch
    ) -> None:
        """A sibling writes first: its message watermark survives, and the reaction it
        already claimed is not claimed twice."""

        class RacingBackend:
            def __init__(self) -> None:
                self.tripped = False
                self.content = json.dumps({**BASE_STATE, REACTION_KEYS_FIELD: []})
                self.etag = "etag-0"
                self.writes = 0

            def read_text_with_etag(self, key: str):
                return self.content, self.etag

            def write_text(self, key, content, *, if_match=None, if_absent=False):
                if not self.tripped:
                    self.tripped = True
                    self.content = json.dumps(
                        {
                            "last_ts": "2026-11-17T11:00:00Z",
                            "seen_ids_tail": ["m1", "sibling"],
                            "bootstrapped": True,
                            REACTION_KEYS_FIELD: [["100", ALICE, THUMBS_UP]],
                        }
                    )
                    self.etag = "etag-sibling"
                    raise ConcurrencyError("simulated 412")
                if if_match != self.etag:
                    raise ConcurrencyError("stale")
                self.writes += 1
                self.content = content
                self.etag = f"etag-{self.writes}"
                return self.etag

        backend = RacingBackend()
        monkeypatch.setattr(cc, "get_backend", lambda: backend)

        claim = claim_reactions(
            CHAT,
            _observation(_key(), _key(user_id=BOB)),
            known=set(),
            base_state=BASE_STATE,
        )

        assert claim.added == (_key(user_id=BOB),)
        final = json.loads(backend.content)
        assert final["last_ts"] == "2026-11-17T11:00:00Z"
        assert final["seen_ids_tail"] == ["m1", "sibling"]
        assert final[REACTION_KEYS_FIELD] == [
            ["100", ALICE, THUMBS_UP],
            ["100", BOB, THUMBS_UP],
        ]

    def test_exhausted_retries_fail_closed(self, tmp_data_dir, monkeypatch) -> None:
        class AlwaysConflicts:
            def read_text_with_etag(self, key: str):
                return json.dumps({**BASE_STATE, REACTION_KEYS_FIELD: []}), "etag"

            def write_text(self, key, content, **kwargs):
                raise ConcurrencyError("412")

        monkeypatch.setattr(cc, "get_backend", lambda: AlwaysConflicts())

        claim = claim_reactions(CHAT, _observation(_key()), known=set(), base_state=BASE_STATE)

        assert claim.resolved is False
        assert claim.added == ()
