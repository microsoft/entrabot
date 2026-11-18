"""The reaction ledger in the per-chat cursor.

It is stored beside ``last_ts`` and ``seen_ids_tail`` but is separate state: the message
ledger never changes it, and it never changes the message ledger. A claim only ever adds to
it, so one instance working from an older read can't erase what another instance claimed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import entrabot.tools.chat_cursors as cc
from entrabot.storage.blob import ConcurrencyError
from entrabot.tools.chat_cursors import (
    REACTION_KEYS_FIELD,
    claim_delivery,
    claim_reactions,
    cursor_key,
    load_cursor,
    release_reactions,
    save_cursor,
)
from entrabot.tools.reactions import (
    REACTION_FLOOR_FIELD,
    MessageReaction,
    ReactionKey,
    ReactionLedger,
    ReactionObservation,
    canonical_timestamp,
)

CHAT = "19:c@thread.v2"
ALICE = "11111111-0000-4000-8000-111111111111"
BOB = "22222222-0000-4000-8000-222222222222"
THUMBS_UP = "\U0001f44d"
NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
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


def _key(
    message_id: str = "100",
    user_id: str = ALICE,
    reaction: str = THUMBS_UP,
    minutes_ago: float = 1,
) -> ReactionKey:
    return ReactionKey(
        message_id, user_id, reaction, canonical_timestamp(NOW - timedelta(minutes=minutes_ago))
    )


R1 = _key(user_id=ALICE, minutes_ago=3)
R2 = _key(user_id=BOB, minutes_ago=2)


def _observation(*keys: ReactionKey) -> ReactionObservation:
    return ReactionObservation(
        reactions={
            key: MessageReaction(key.reaction_type, "", key.user_id, key.created_at)
            for key in keys
        }
    )


def _ledger(*keys: ReactionKey) -> ReactionLedger:
    return ReactionLedger(keys=frozenset(keys))


def _json(*keys: ReactionKey) -> list[list[str]]:
    return _ledger(*keys).to_cursor_fields()[REACTION_KEYS_FIELD]


def _stored_ledger(chat_id: str = CHAT) -> ReactionLedger | None:
    return ReactionLedger.from_cursor(load_cursor(chat_id))


def _claim(observation: ReactionObservation, known: ReactionLedger | None, **kwargs):
    kwargs.setdefault("base_state", BASE_STATE)
    return claim_reactions(CHAT, observation, known=known, now=NOW, **kwargs)


class TestSaveCursorKeepsTheLedger:
    def test_a_ledger_in_the_state_is_written(self, tmp_data_dir) -> None:
        save_cursor(CHAT, {**BASE_STATE, **_ledger(R1).to_cursor_fields()})
        assert _stored_ledger() == _ledger(R1)

    def test_the_floor_is_written_with_it(self, tmp_data_dir) -> None:
        ledger = ReactionLedger(keys=frozenset({R1}), floor=canonical_timestamp(NOW))
        save_cursor(CHAT, {**BASE_STATE, **ledger.to_cursor_fields()})
        assert _stored_ledger() == ledger

    def test_no_ledger_in_the_state_writes_none(self, tmp_data_dir) -> None:
        """Absent, not empty: an empty list would say the state had been built."""
        save_cursor(CHAT, BASE_STATE)
        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert REACTION_KEYS_FIELD not in cursor
        assert REACTION_FLOOR_FIELD not in cursor


class TestClaimDeliveryLeavesTheLedgerAlone:
    def test_a_message_claim_keeps_the_stored_ledger(self, tmp_data_dir) -> None:
        ledger = ReactionLedger(keys=frozenset({R1}), floor=canonical_timestamp(NOW))
        save_cursor(CHAT, {**BASE_STATE, **ledger.to_cursor_fields()})

        assert claim_delivery(CHAT, ["m2"], "2026-11-17T09:05:00Z") == ["m2"]

        assert _stored_ledger() == ledger

    def test_a_message_claim_does_not_invent_a_ledger(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)
        claim_delivery(CHAT, ["m2"], "2026-11-17T09:05:00Z")
        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert REACTION_KEYS_FIELD not in cursor


class TestClaimReactions:
    def test_the_first_build_seeds_silently(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)

        claim = _claim(_observation(R1), None)

        assert claim.resolved is True
        assert claim.added == ()
        assert claim.ledger == _ledger(R1)
        assert _stored_ledger() == _ledger(R1)

    def test_an_empty_first_build_is_stored_as_empty_not_missing(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)
        _claim(_observation(), None)
        assert _stored_ledger() == ReactionLedger()

    def test_a_reaction_added_after_the_seed_is_claimed_once(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)
        _claim(_observation(), None)

        first = _claim(_observation(R1), ReactionLedger())
        again = _claim(_observation(R1), ReactionLedger())

        assert first.added == (R1,)
        assert again.resolved is True
        assert again.added == ()
        assert again.ledger == _ledger(R1)

    def test_the_message_cursor_is_left_exactly_as_it_was(self, tmp_data_dir) -> None:
        save_cursor(CHAT, BASE_STATE)

        _claim(_observation(R1), None, base_state={})

        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert cursor["last_ts"] == BASE_STATE["last_ts"]
        assert cursor["seen_ids_tail"] == ["m1"]
        assert cursor["bootstrapped"] is True

    def test_an_observation_that_lacks_a_reaction_does_not_remove_it(self, tmp_data_dir) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: _json(R1, R2)})

        claim = _claim(_observation(R1), _ledger(R1))

        assert claim.added == ()
        assert _stored_ledger() == _ledger(R1, R2)
        assert claim.ledger == _ledger(R1, R2), "the caller learns what a sibling claimed"

    def test_two_pollers_one_behind_the_other_announce_each_reaction_once(
        self, tmp_data_dir
    ) -> None:
        """R1 is added and instance A reads the chat. R2 is added and instance B reads it.
        B claims first; A's claim, made from the older read, lands second. A must not undo
        B's claim of R2, or A would announce R2 again on its next read."""
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: []})
        a_read = _observation(R1)
        b_read = _observation(R1, R2)

        b = _claim(b_read, ReactionLedger())
        a = _claim(a_read, ReactionLedger())
        a_next = _claim(_observation(R1, R2), a.ledger)

        assert b.added == (R1, R2)
        assert a.added == ()
        assert _stored_ledger() == _ledger(R1, R2)
        assert a_next.added == ()
        assert [*b.added, *a.added, *a_next.added] == [R1, R2]

    def test_a_claim_that_writes_keeps_what_its_read_did_not_show(self, tmp_data_dir) -> None:
        """The dangerous case is a claim that has something to write. A's read lacks R2,
        which B already claimed, but has R3, which nobody has. Writing R3 must not drop R2."""
        r3 = _key(message_id="101", user_id="carol", minutes_ago=1)
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: _json(R1, R2)})

        claim = _claim(_observation(R1, r3), _ledger(R1))

        assert claim.added == (r3,)
        assert _stored_ledger() == _ledger(R1, R2, r3)
        assert _claim(_observation(R1, R2, r3), claim.ledger).added == ()

    def test_a_re_add_is_a_new_key_and_is_claimed(self, tmp_data_dir) -> None:
        first = _key(minutes_ago=10)
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: _json(first)})
        again = _key(minutes_ago=1)

        claim = _claim(_observation(again), _ledger(first))

        assert claim.added == (again,)
        assert _stored_ledger() == _ledger(first, again)

    def test_nothing_is_written_when_nothing_is_new(self, tmp_data_dir, monkeypatch) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: _json(R1)})
        before = load_cursor(CHAT)
        backend = cc.get_backend()
        writes: list[str] = []
        real_write = backend.write_text

        def counting_write(key, content, **kwargs):
            writes.append(key)
            return real_write(key, content, **kwargs)

        monkeypatch.setattr(backend, "write_text", counting_write)
        monkeypatch.setattr(cc, "get_backend", lambda: backend)

        claim = _claim(_observation(R1), None)

        assert claim.resolved is True and claim.added == ()
        assert writes == []
        assert load_cursor(CHAT) == before

    def test_a_ledger_this_process_built_is_the_baseline_when_storage_lost_it(
        self, tmp_data_dir
    ) -> None:
        """Something rewrites the cursor without the ledger. What this process already
        knows still tells a new reaction from an old one."""
        save_cursor(CHAT, BASE_STATE)  # no ledger stored

        claim = _claim(_observation(R1, R2), _ledger(R1))

        assert claim.added == (R2,)
        assert _stored_ledger() == _ledger(R1, R2)

    def test_two_instances_one_push(self, tmp_data_dir, monkeypatch) -> None:
        from entrabot.storage.backend import BlobBackend
        from tests.storage.test_backend import _FakeBlobStore

        shared = _FakeBlobStore()
        monkeypatch.setattr(cc, "get_backend", lambda: BlobBackend(shared))
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: []})

        a = _claim(_observation(R1), ReactionLedger())
        b = _claim(_observation(R1), ReactionLedger())

        assert [*a.added, *b.added] == [R1]
        assert b.resolved is True and b.ledger == _ledger(R1)

    def test_a_ledger_from_before_the_timestamp_was_in_the_key_is_rebuilt_silently(
        self, tmp_data_dir
    ) -> None:
        save_cursor(CHAT, BASE_STATE)
        cursor = load_cursor(CHAT)
        assert cursor is not None
        cursor[REACTION_KEYS_FIELD] = [["100", ALICE, THUMBS_UP]]
        cc.get_backend().write_text(cursor_key(CHAT), json.dumps(cursor))

        claim = _claim(_observation(R1), None)

        assert claim.added == ()
        assert _stored_ledger() == _ledger(R1)

    def test_a_missing_cursor_is_created_from_the_in_memory_state(self, tmp_data_dir) -> None:
        claim = _claim(_observation(R1), None)

        assert claim.resolved is True and claim.added == ()
        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert cursor["last_ts"] == BASE_STATE["last_ts"]
        assert cursor["seen_ids_tail"] == ["m1"]
        assert cursor["bootstrapped"] is True
        assert _stored_ledger() == _ledger(R1)

    def test_a_missing_cursor_is_never_created_without_a_watermark(self, tmp_data_dir) -> None:
        """A cursor with no ``last_ts`` would make the next restart replay the chat."""
        claim = _claim(_observation(R1), None, base_state={"bootstrapped": True})

        assert claim.resolved is False
        assert claim.added == ()
        assert load_cursor(CHAT) is None

    def test_a_read_failure_fails_closed(self, tmp_data_dir, monkeypatch) -> None:
        class ExplodingBackend:
            def read_text_with_etag(self, key: str):
                raise OSError("transient read failure")

        monkeypatch.setattr(cc, "get_backend", lambda: ExplodingBackend())

        claim = _claim(_observation(R1), ReactionLedger())

        assert claim.resolved is False
        assert claim.added == ()

    @pytest.mark.parametrize("payload", ["{not json", "[1, 2, 3]", "null"])
    def test_a_corrupt_cursor_fails_closed_and_is_not_overwritten(
        self, tmp_data_dir, payload: str
    ) -> None:
        cc.get_backend().write_text(cursor_key(CHAT), payload)

        claim = _claim(_observation(R1), ReactionLedger())

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

        claim = _claim(_observation(R1), ReactionLedger())

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
                            REACTION_KEYS_FIELD: _json(R1),
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

        claim = _claim(_observation(R1, R2), ReactionLedger())

        assert claim.added == (R2,)
        final = json.loads(backend.content)
        assert final["last_ts"] == "2026-11-17T11:00:00Z"
        assert final["seen_ids_tail"] == ["m1", "sibling"]
        assert final[REACTION_KEYS_FIELD] == _json(R1, R2)

    def test_exhausted_retries_fail_closed(self, tmp_data_dir, monkeypatch) -> None:
        class AlwaysConflicts:
            def read_text_with_etag(self, key: str):
                return json.dumps({**BASE_STATE, REACTION_KEYS_FIELD: []}), "etag"

            def write_text(self, key, content, **kwargs):
                raise ConcurrencyError("412")

        monkeypatch.setattr(cc, "get_backend", lambda: AlwaysConflicts())

        claim = _claim(_observation(R1), ReactionLedger())

        assert claim.resolved is False
        assert claim.added == ()

    def test_the_default_clock_is_the_real_one(self, tmp_data_dir) -> None:
        """A key from the fixed test clock is far older than the retention, so it is dropped."""
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: []})
        ancient = ReactionKey(
            "100", ALICE, THUMBS_UP, canonical_timestamp(datetime(2020, 1, 1, tzinfo=UTC))
        )

        claim = claim_reactions(
            CHAT, _observation(ancient), known=ReactionLedger(), base_state=BASE_STATE
        )

        assert claim.added == (ancient,)
        assert _stored_ledger() == ReactionLedger()


class TestReleaseReactions:
    """A claim whose push failed is taken back, so the next cycle can try again."""

    def test_exactly_the_released_keys_leave_the_ledger(self, tmp_data_dir) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: _json(R1, R2)})

        assert release_reactions(CHAT, [R2]) is True

        assert _stored_ledger() == _ledger(R1)
        cursor = load_cursor(CHAT)
        assert cursor is not None
        assert cursor["last_ts"] == BASE_STATE["last_ts"]
        assert cursor["seen_ids_tail"] == ["m1"]

    def test_a_released_reaction_can_be_claimed_again(self, tmp_data_dir) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: []})
        first = _claim(_observation(R1), ReactionLedger())
        release_reactions(CHAT, first.added)

        again = _claim(_observation(R1), ReactionLedger())

        assert again.added == (R1,)

    def test_a_key_a_sibling_added_meanwhile_is_kept(self, tmp_data_dir) -> None:
        """It is a removal of named keys, not a rewrite from what the caller last saw."""
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: _json(R1)})
        _claim(_observation(R1, R2), _ledger(R1))  # a sibling claims R2

        release_reactions(CHAT, [R1])

        assert _stored_ledger() == _ledger(R2)

    def test_nothing_to_release_writes_nothing(self, tmp_data_dir) -> None:
        save_cursor(CHAT, {**BASE_STATE, REACTION_KEYS_FIELD: _json(R1)})
        before = load_cursor(CHAT)

        assert release_reactions(CHAT, []) is True
        assert release_reactions(CHAT, [R2]) is True

        assert load_cursor(CHAT) == before

    def test_no_cursor_is_nothing_to_release(self, tmp_data_dir) -> None:
        assert release_reactions(CHAT, [R1]) is True
        assert load_cursor(CHAT) is None

    def test_a_storage_failure_is_reported(self, tmp_data_dir, monkeypatch) -> None:
        class ExplodingBackend:
            def read_text_with_etag(self, key: str):
                raise OSError("transient read failure")

        monkeypatch.setattr(cc, "get_backend", lambda: ExplodingBackend())

        assert release_reactions(CHAT, [R1]) is False
