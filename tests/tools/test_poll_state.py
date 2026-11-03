"""Tests for shared poll state: the watched-chat list and the email cursor.

Both values follow the configured ``MemoryBackend``. In cloud mode they are
shared across instances and never touch local files. The rules that matter here
are the fleet-safety ones from ``DESIGN-multi-instance-cursor-consistency.md``:
a read failure is never treated as absent, an add never removes another
instance's chats, and a stale cursor can never replay old mail.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

from entrabot.storage.backend import BlobBackend, LocalBackend
from entrabot.storage.blob import ConcurrencyError
from entrabot.tools import poll_state
from entrabot.tools.email_poll import advance_cursor
from entrabot.tools.poll_state import (
    EMAIL_CURSOR_KEY,
    LEGACY_EMAIL_CURSOR_KEY,
    WATCHED_CHATS_KEY,
    EmailClaim,
    PollStateOutcome,
    add_watched_chats,
    claim_email_window,
    ignored_legacy_local_copies,
    initialize_email_cursor,
    read_email_cursor,
    read_watched_chats,
)

CHAT_A = "19:chat-a@thread.v2"
CHAT_B = "19:chat-b@thread.v2"
CHAT_C = "19:chat-c_d@unq.gbl.spaces"
NOW = "2026-11-02T12:00:00Z"
STALE = "2026-05-01T00:00:00Z"
LATER = "2026-11-02T12:05:00.001Z"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "agent-data"
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(path))
    return path


def _use_backend(monkeypatch: pytest.MonkeyPatch, backend: object) -> None:
    monkeypatch.setattr(poll_state, "get_backend", lambda: backend)


class _ReadFails:
    def read_text(self, key: str) -> str | None:
        raise OSError("simulated blob outage")

    def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]:
        raise OSError("simulated blob outage")

    def write_text(self, *args: object, **kwargs: object) -> str | None:
        raise AssertionError("must not write after a failed read")


class _WriteFails(LocalBackend):
    def write_text(self, *args: object, **kwargs: object) -> str | None:
        raise OSError("simulated blob outage")


class _NoEtag:
    """A store returning content without an ETag, so a CAS isn't possible."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.writes: list[tuple] = []

    def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]:
        return self.content, ""

    def write_text(self, *args: object, **kwargs: object) -> str | None:
        self.writes.append((args, kwargs))
        return "etag"


# ---------------------------------------------------------------------------
# read_watched_chats
# ---------------------------------------------------------------------------
class _ErrorAfterWrite:
    """Commits the write, then raises as if the response had been lost."""

    def __init__(
        self, value: str, *, error: Exception | None = None, reread_fails: bool = False
    ) -> None:
        self.value = value
        self.error = error or OSError("connection reset")
        self.reread_fails = reread_fails
        self.armed = False

    def arm_reread_failure(self) -> None:
        self.armed = self.reread_fails

    def read_text_with_etag(self, key: str) -> tuple[str, str]:
        return self.value, '"etag"'

    def read_text(self, key: str) -> str:
        if self.armed:
            raise OSError("still down")
        return self.value

    def write_text(self, key: str, content: str, *, if_match: str | None = None) -> str:
        self.value = content
        raise self.error


class _SiblingClaimedFirst(_ErrorAfterWrite):
    """Our write fails without landing, and a sibling stores the same cursor."""

    def __init__(self, value: str, *, sibling_value: str) -> None:
        super().__init__(value)
        self.sibling_value = sibling_value

    def write_text(self, key: str, content: str, *, if_match: str | None = None) -> str:
        self.value = f"{self.sibling_value}\nsibling-claim"
        raise self.error


class TestReadWatchedChats:
    def test_absent_is_absent_not_unresolved(self, fake_cloud_store) -> None:
        result = read_watched_chats()
        assert result.outcome is PollStateOutcome.ABSENT
        assert result.chat_ids == ()
        assert result.etag is None

    def test_present_returns_ids_and_etag(self, fake_cloud_store) -> None:
        etag = BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{CHAT_A}\n{CHAT_B}\n")
        result = read_watched_chats()
        assert result.outcome is PollStateOutcome.PRESENT
        assert result.chat_ids == (CHAT_A, CHAT_B)
        assert result.etag == etag

    def test_blank_lines_whitespace_and_duplicates_are_normalized(self, fake_cloud_store) -> None:
        BlobBackend(fake_cloud_store).write_text(
            WATCHED_CHATS_KEY, f"\n  {CHAT_A} \n\n{CHAT_B}\n{CHAT_A}\n"
        )
        assert read_watched_chats().chat_ids == (CHAT_A, CHAT_B)

    def test_empty_list_is_present_with_no_chats(self, fake_cloud_store) -> None:
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, "")
        result = read_watched_chats()
        assert result.outcome is PollStateOutcome.PRESENT
        assert result.chat_ids == ()

    def test_read_failure_is_unresolved_never_absent(self, monkeypatch) -> None:
        _use_backend(monkeypatch, _ReadFails())
        result = read_watched_chats()
        assert result.outcome is PollStateOutcome.UNRESOLVED
        assert result.chat_ids == ()

    def test_backend_resolution_failure_is_unresolved(self, monkeypatch) -> None:
        def boom() -> object:
            raise RuntimeError("half-configured backend")

        monkeypatch.setattr(poll_state, "get_backend", boom)
        assert read_watched_chats().outcome is PollStateOutcome.UNRESOLVED

    def test_undecodable_content_is_unresolved(self, fake_cloud_store) -> None:
        fake_cloud_store.data[WATCHED_CHATS_KEY] = b"\xff\xfe not utf-8"
        fake_cloud_store.etags[WATCHED_CHATS_KEY] = '"etag"'
        assert read_watched_chats().outcome is PollStateOutcome.UNRESOLVED

    @pytest.mark.parametrize(
        "line",
        [
            "../../users/someone",
            "19:has/slash@thread.v2",
            "19:has space@thread.v2",
            "19:query?x=1@thread.v2",
            "19:frag#x@thread.v2",
            "19:pct%2e@thread.v2",
            "19:back\\slash@thread.v2",
            "19:ctrl\x07@thread.v2",
        ],
    )
    def test_entry_that_cannot_be_a_chat_id_is_dropped(
        self, fake_cloud_store, line: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The list is shared storage, and each ID ends up in a Graph URL path."""
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{CHAT_A}\n{line}\n")
        assert read_watched_chats().chat_ids == (CHAT_A,)
        assert "malformed" in caplog.text


# ---------------------------------------------------------------------------
# add_watched_chats
# ---------------------------------------------------------------------------
class TestAddWatchedChats:
    def test_creates_list_when_absent_in_sorted_newline_format(self, fake_cloud_store) -> None:
        result = add_watched_chats([CHAT_B, CHAT_A])
        assert result.persisted is True
        assert result.added == (CHAT_A, CHAT_B)
        assert fake_cloud_store.data[WATCHED_CHATS_KEY].decode() == f"{CHAT_A}\n{CHAT_B}\n"

    def test_merges_into_existing_list(self, fake_cloud_store) -> None:
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{CHAT_B}\n")
        result = add_watched_chats([CHAT_A])
        assert result.persisted is True
        assert result.added == (CHAT_A,)
        assert read_watched_chats().chat_ids == (CHAT_A, CHAT_B)

    def test_already_listed_chat_does_not_write(self, fake_cloud_store) -> None:
        etag = BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{CHAT_A}\n")
        result = add_watched_chats([CHAT_A])
        assert result.persisted is True
        assert result.added == ()
        assert fake_cloud_store.etags[WATCHED_CHATS_KEY] == etag

    def test_empty_batch_does_not_touch_the_backend(self, monkeypatch) -> None:
        _use_backend(monkeypatch, _ReadFails())
        result = add_watched_chats([])
        assert result.persisted is True
        assert result.added == ()

    def test_malformed_ids_are_not_written(self, fake_cloud_store) -> None:
        result = add_watched_chats([CHAT_A, "bad/id"])
        assert result.persisted is True
        assert result.added == (CHAT_A,)
        assert fake_cloud_store.data[WATCHED_CHATS_KEY].decode() == f"{CHAT_A}\n"

    def test_concurrent_updates_keep_the_union(self, fake_cloud_store, monkeypatch) -> None:
        """Both instances read the same ETag before either writes."""
        BlobBackend(fake_cloud_store).write_text(WATCHED_CHATS_KEY, f"{CHAT_C}\n")
        both_have_read = threading.Barrier(2, timeout=5)
        waited = threading.local()

        class _ReadThenWait(BlobBackend):
            def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]:
                result = super().read_text_with_etag(key)
                if not getattr(waited, "done", False):
                    waited.done = True
                    both_have_read.wait()
                return result

        _use_backend(monkeypatch, _ReadThenWait(fake_cloud_store))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda cid: add_watched_chats([cid]), [CHAT_A, CHAT_B]))

        assert all(r.persisted for r in results)
        stored = fake_cloud_store.data[WATCHED_CHATS_KEY].decode().splitlines()
        assert stored == [CHAT_A, CHAT_B, CHAT_C]

    def test_concurrent_first_create_keeps_the_union(self, fake_cloud_store, monkeypatch) -> None:
        """Both instances read ABSENT. An unconditional first write would lose one."""
        both_have_read = threading.Barrier(2, timeout=5)
        waited = threading.local()

        class _ReadThenWait(BlobBackend):
            def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]:
                result = super().read_text_with_etag(key)
                if not getattr(waited, "done", False):
                    waited.done = True
                    both_have_read.wait()
                return result

        _use_backend(monkeypatch, _ReadThenWait(fake_cloud_store))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda cid: add_watched_chats([cid]), [CHAT_A, CHAT_B]))

        assert all(r.persisted for r in results)
        stored = fake_cloud_store.data[WATCHED_CHATS_KEY].decode().splitlines()
        assert stored == [CHAT_A, CHAT_B]

    def test_exhausted_cas_retries_report_failure_without_raising(self, monkeypatch) -> None:
        attempts: list[str] = []

        class _AlwaysRaced:
            def read_text_with_etag(self, key: str) -> tuple[str | None, str | None]:
                return f"{CHAT_C}\n", f'"etag-{len(attempts)}"'

            def write_text(self, key: str, content: str, **kwargs: object) -> str | None:
                attempts.append(content)
                raise ConcurrencyError("simulated 412")

        _use_backend(monkeypatch, _AlwaysRaced())
        result = add_watched_chats([CHAT_A])
        assert result.persisted is False
        assert result.added == ()
        assert len(attempts) == poll_state.WATCHED_CHATS_MAX_ATTEMPTS

    def test_read_failure_writes_nothing(self, monkeypatch) -> None:
        """A failed read must never be taken for an empty list and overwrite it."""
        _use_backend(monkeypatch, _ReadFails())
        result = add_watched_chats([CHAT_A])
        assert result.persisted is False

    def test_non_conflict_write_failure_reports_failure(self, data_dir, monkeypatch) -> None:
        _use_backend(monkeypatch, _WriteFails(data_dir))
        result = add_watched_chats([CHAT_A])
        assert result.persisted is False

    def test_present_without_etag_is_never_written_unconditionally(self, monkeypatch) -> None:
        backend = _NoEtag(f"{CHAT_C}\n")
        _use_backend(monkeypatch, backend)
        result = add_watched_chats([CHAT_A])
        assert result.persisted is False
        assert backend.writes == []


# ---------------------------------------------------------------------------
# Local mode keeps today's file
# ---------------------------------------------------------------------------
class TestLocalWatchedChats:
    def test_key_is_the_existing_local_file(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{CHAT_B}\n{CHAT_A}\n")
        assert read_watched_chats().chat_ids == (CHAT_B, CHAT_A)

    def test_add_writes_the_same_path_and_format_as_before(self, data_dir: Path) -> None:
        assert add_watched_chats([CHAT_B, CHAT_A]).persisted is True
        assert (data_dir / "watched_chats").read_text() == f"{CHAT_A}\n{CHAT_B}\n"

    def test_add_merges_with_existing_file(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{CHAT_C}\n")
        add_watched_chats([CHAT_A])
        assert (data_dir / "watched_chats").read_text() == f"{CHAT_A}\n{CHAT_C}\n"


# ---------------------------------------------------------------------------
# read_email_cursor
# ---------------------------------------------------------------------------
class TestReadEmailCursor:
    def test_absent(self, fake_cloud_store) -> None:
        result = read_email_cursor()
        assert result.outcome is PollStateOutcome.ABSENT
        assert result.cursor is None
        assert result.etag is None

    def test_present_with_etag_and_whitespace_stripped(self, fake_cloud_store) -> None:
        etag = BlobBackend(fake_cloud_store).write_text(EMAIL_CURSOR_KEY, f"  {NOW}\n")
        result = read_email_cursor()
        assert result.outcome is PollStateOutcome.PRESENT
        assert result.cursor == NOW
        assert result.etag == etag

    def test_read_failure_is_unresolved_never_absent(self, monkeypatch) -> None:
        _use_backend(monkeypatch, _ReadFails())
        assert read_email_cursor().outcome is PollStateOutcome.UNRESOLVED

    @pytest.mark.parametrize(
        "corrupt",
        [
            "",
            "   \n",
            "cursor=42",
            "<html>proxy</html>",
            "2026-11-02",  # parses, but Graph can't filter on a date with no time
            "2026-11-02T12:00:00",  # no timezone
            "2026-11-02 12:00:00Z",  # not RFC 3339's T separator
        ],
    )
    def test_corrupt_value_is_unresolved(self, fake_cloud_store, corrupt: str) -> None:
        """Mirrors chat_cursors: a corrupt value is ambiguous, not a clean slate."""
        BlobBackend(fake_cloud_store).write_text(EMAIL_CURSOR_KEY, corrupt)
        assert read_email_cursor().outcome is PollStateOutcome.UNRESOLVED

    def test_present_without_etag_is_unresolved(self, monkeypatch) -> None:
        """Without an ETag the window can't be claimed, so it can't be pushed."""
        _use_backend(monkeypatch, _NoEtag(NOW))
        assert read_email_cursor().outcome is PollStateOutcome.UNRESOLVED

    def test_never_reads_the_old_cloud_key(self, fake_cloud_store) -> None:
        BlobBackend(fake_cloud_store).write_text(LEGACY_EMAIL_CURSOR_KEY, STALE)
        result = read_email_cursor()
        assert result.outcome is PollStateOutcome.ABSENT
        assert result.cursor is None


# ---------------------------------------------------------------------------
# initialize_email_cursor
# ---------------------------------------------------------------------------
class TestInitializeEmailCursorCloud:
    def test_initializes_to_now(self, fake_cloud_store) -> None:
        assert initialize_email_cursor(NOW) is True
        assert read_email_cursor().cursor == NOW

    def test_default_now_is_a_valid_cursor(self, fake_cloud_store) -> None:
        assert initialize_email_cursor() is True
        result = read_email_cursor()
        assert result.outcome is PollStateOutcome.PRESENT
        assert result.cursor is not None and result.cursor.endswith("Z")

    def test_stale_old_cloud_key_is_ignored(self, fake_cloud_store) -> None:
        """setup's migration left this blob months ago; adopting it replays mail."""
        BlobBackend(fake_cloud_store).write_text(LEGACY_EMAIL_CURSOR_KEY, STALE)
        initialize_email_cursor(NOW)
        assert read_email_cursor().cursor == NOW
        assert fake_cloud_store.data[LEGACY_EMAIL_CURSOR_KEY].decode() == STALE

    def test_stale_local_file_is_ignored(self, fake_cloud_store, data_dir: Path) -> None:
        """The near-miss: a machine moved to another container keeps its old file."""
        data_dir.mkdir(parents=True)
        (data_dir / "email_cursor.txt").write_text(STALE)
        initialize_email_cursor(NOW)
        assert read_email_cursor().cursor == NOW
        assert (data_dir / "email_cursor.txt").read_text() == STALE
        assert not (data_dir / EMAIL_CURSOR_KEY).exists()

    def test_losing_the_create_race_keeps_the_winner(self, fake_cloud_store) -> None:
        BlobBackend(fake_cloud_store).write_text(EMAIL_CURSOR_KEY, LATER)
        assert initialize_email_cursor(NOW) is True
        assert read_email_cursor().cursor == LATER

    def test_write_failure_reports_false(self, monkeypatch, data_dir: Path) -> None:
        class _CloudWriteFails:
            def write_text(self, *args: object, **kwargs: object) -> str | None:
                raise OSError("simulated blob outage")

        _use_backend(monkeypatch, _CloudWriteFails())
        assert initialize_email_cursor(NOW) is False


class _Clock(datetime):
    """``datetime`` with a fixed ``now()``: 750 ms into a second."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime(2026, 11, 2, 12, 0, 0, 750_000, tzinfo=UTC)


def _instant(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


class TestDefaultCursorPrecision:
    """A fresh cursor starts at now. Whole seconds would round down, so mail received
    earlier in the same second would be newer than the cursor and a first boot could
    replay it. That is a replay of mail nobody has seen pushed, which is the failure
    this module exists to prevent."""

    @pytest.fixture(autouse=True)
    def _freeze_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(poll_state, "datetime", _Clock)

    def test_default_cursor_is_stamped_to_the_millisecond(self, fake_cloud_store) -> None:
        assert initialize_email_cursor() is True
        assert read_email_cursor().cursor == "2026-11-02T12:00:00.750Z"

    def test_default_cursor_has_the_format_advance_cursor_produces(self) -> None:
        cursor = poll_state._now_cursor()
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", cursor)
        # advance_cursor accepts it and keeps the same shape, so a fresh cursor and an
        # advanced one are interchangeable to everything downstream.
        assert advance_cursor(cursor) == "2026-11-02T12:00:00.751Z"

    def test_mail_from_earlier_in_the_same_second_is_not_newer_than_the_cursor(
        self, fake_cloud_store
    ) -> None:
        initialize_email_cursor()
        cursor = read_email_cursor().cursor

        for earlier in ("2026-11-02T12:00:00Z", "2026-11-02T12:00:00.400Z"):
            assert _instant(earlier) <= _instant(cursor), earlier
        # Mail that really arrives afterwards is still new.
        assert _instant("2026-11-02T12:00:00.800Z") > _instant(cursor)

    def test_default_cursor_is_accepted_by_the_read_path(self, fake_cloud_store) -> None:
        initialize_email_cursor()
        assert read_email_cursor().outcome is PollStateOutcome.PRESENT


class TestInitializeEmailCursorLocal:
    def test_without_legacy_file_initializes_to_now(self, data_dir: Path) -> None:
        assert initialize_email_cursor(NOW) is True
        assert read_email_cursor().cursor == NOW
        assert (data_dir / "poll_state" / "email_cursor.txt").read_text() == NOW

    def test_adopts_legacy_file_and_leaves_it_in_place(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "email_cursor.txt").write_text(f"{STALE}\n")
        assert initialize_email_cursor(NOW) is True
        assert read_email_cursor().cursor == STALE
        assert (data_dir / "email_cursor.txt").read_text() == f"{STALE}\n"

    def test_adopts_only_once(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "email_cursor.txt").write_text(STALE)
        initialize_email_cursor(NOW)
        etag = read_email_cursor().etag
        assert claim_email_window(LATER, etag) is EmailClaim.WON
        # A later change to the legacy file has no effect any more.
        (data_dir / "email_cursor.txt").write_text("2026-01-01T00:00:00Z")
        initialize_email_cursor(NOW)
        assert read_email_cursor().cursor == LATER

    @pytest.mark.parametrize("legacy", ["", "   \n", "not-a-timestamp"])
    def test_unusable_legacy_value_initializes_to_now(self, data_dir: Path, legacy: str) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "email_cursor.txt").write_text(legacy)
        assert initialize_email_cursor(NOW) is True
        assert read_email_cursor().cursor == NOW

    def test_legacy_read_failure_writes_nothing_and_retries(
        self, data_dir: Path, monkeypatch
    ) -> None:
        """An unreadable legacy file is not "no legacy file": starting at now
        would drop the local user's gap, so leave the key absent for a retry."""

        class _LegacyReadFails(LocalBackend):
            def read_text(self, key: str) -> str | None:
                raise OSError("simulated disk failure")

        _use_backend(monkeypatch, _LegacyReadFails(data_dir))
        assert initialize_email_cursor(NOW) is False
        assert not (data_dir / EMAIL_CURSOR_KEY).exists()


# ---------------------------------------------------------------------------
# claim_email_window
# ---------------------------------------------------------------------------
class TestClaimEmailWindow:
    def test_winning_advances_the_cursor(self, fake_cloud_store) -> None:
        initialize_email_cursor(NOW)
        assert claim_email_window(LATER, read_email_cursor().etag) is EmailClaim.WON
        assert read_email_cursor().cursor == LATER

    def test_strips_whitespace_before_storing(self, fake_cloud_store) -> None:
        initialize_email_cursor(NOW)
        claim_email_window(f"  {LATER}  \n", read_email_cursor().etag)
        assert read_email_cursor().cursor == LATER

    def test_stale_etag_loses_and_leaves_the_cursor_alone(self, fake_cloud_store) -> None:
        initialize_email_cursor(NOW)
        stale_etag = read_email_cursor().etag
        BlobBackend(fake_cloud_store).write_text(EMAIL_CURSOR_KEY, LATER)
        assert claim_email_window("2026-11-02T12:01:00Z", stale_etag) is EmailClaim.LOST
        assert read_email_cursor().cursor == LATER

    def test_two_instances_one_winner(self, fake_cloud_store) -> None:
        initialize_email_cursor(NOW)
        etag = read_email_cursor().etag
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: claim_email_window(LATER, etag), range(2)))
        assert sorted(o.name for o in outcomes) == ["LOST", "WON"]

    def test_non_conflict_failure_is_failed_not_lost(self, data_dir, monkeypatch) -> None:
        _use_backend(monkeypatch, _WriteFails(data_dir))
        assert claim_email_window(LATER, '"etag"') is EmailClaim.FAILED

    def test_a_write_that_landed_despite_an_error_still_wins(self, monkeypatch) -> None:
        """A transport error doesn't prove the PUT failed. If it committed and this
        batch were dropped, the next poll would read the advanced cursor and never
        deliver that mail."""
        backend = _ErrorAfterWrite(NOW)
        _use_backend(monkeypatch, backend)
        assert claim_email_window(LATER, read_email_cursor().etag) is EmailClaim.WON
        assert read_email_cursor().cursor == LATER

    def test_a_retried_write_that_landed_is_not_mistaken_for_a_lost_race(self, monkeypatch) -> None:
        backend = _ErrorAfterWrite(NOW, error=ConcurrencyError("412 on a retry"))
        _use_backend(monkeypatch, backend)
        assert claim_email_window(LATER, read_email_cursor().etag) is EmailClaim.WON

    def test_a_sibling_writing_the_same_cursor_is_not_our_claim(self, monkeypatch) -> None:
        """Both instances compute the same cursor from the same batch, so the value alone
        can't say who claimed it. Treating it as ours would push the batch twice."""
        backend = _SiblingClaimedFirst(NOW, sibling_value=LATER)
        _use_backend(monkeypatch, backend)
        assert claim_email_window(LATER, read_email_cursor().etag) is EmailClaim.FAILED

    def test_an_ambiguous_write_that_cannot_be_reread_fails_closed(self, monkeypatch) -> None:
        backend = _ErrorAfterWrite(NOW, reread_fails=True)
        _use_backend(monkeypatch, backend)
        etag = read_email_cursor().etag
        backend.arm_reread_failure()
        assert claim_email_window(LATER, etag) is EmailClaim.FAILED

    @pytest.mark.parametrize("etag", [None, ""])
    def test_missing_etag_never_writes_unconditionally(self, monkeypatch, etag) -> None:
        backend = _NoEtag(NOW)
        _use_backend(monkeypatch, backend)
        assert claim_email_window(LATER, etag) is EmailClaim.FAILED
        assert backend.writes == []


# ---------------------------------------------------------------------------
# Cloud mode leaves local disk alone
# ---------------------------------------------------------------------------
class TestCloudModeNeverTouchesLocalFiles:
    def test_full_cycle_creates_no_local_files(self, fake_cloud_store, data_dir: Path) -> None:
        add_watched_chats([CHAT_A])
        read_watched_chats()
        initialize_email_cursor(NOW)
        claim_email_window(LATER, read_email_cursor().etag)

        assert not data_dir.exists() or list(data_dir.rglob("*")) == []
        assert set(fake_cloud_store.data) == {WATCHED_CHATS_KEY, EMAIL_CURSOR_KEY}

    def test_stale_local_watched_chats_are_not_loaded(
        self, fake_cloud_store, data_dir: Path
    ) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text("19:old-agent-chat@thread.v2\n")
        result = read_watched_chats()
        assert result.outcome is PollStateOutcome.ABSENT
        assert result.chat_ids == ()
        add_watched_chats([CHAT_A])
        assert read_watched_chats().chat_ids == (CHAT_A,)
        assert (data_dir / "watched_chats").read_text() == "19:old-agent-chat@thread.v2\n"


class TestIgnoredLegacyLocalCopies:
    def test_cloud_mode_lists_existing_copies(self, fake_cloud_store, data_dir: Path) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{CHAT_A}\n")
        (data_dir / "email_cursor.txt").write_text(STALE)
        assert sorted(p.name for p in ignored_legacy_local_copies()) == [
            "email_cursor.txt",
            "watched_chats",
        ]

    def test_cloud_mode_with_no_copies_lists_nothing(self, fake_cloud_store, data_dir) -> None:
        assert ignored_legacy_local_copies() == []

    def test_local_mode_lists_nothing_because_they_are_in_use(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "watched_chats").write_text(f"{CHAT_A}\n")
        (data_dir / "email_cursor.txt").write_text(STALE)
        assert ignored_legacy_local_copies() == []

    def test_never_raises(self, monkeypatch) -> None:
        def boom() -> object:
            raise RuntimeError("half-configured backend")

        monkeypatch.setattr(poll_state, "get_backend", boom)
        assert ignored_legacy_local_copies() == []
