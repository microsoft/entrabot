"""Tests for the fleet-safe email poll cycle in :mod:`entrabot.mcp_server`.

Each cycle reads the shared cursor with its ETag, fetches from Graph, and claims
the window with a compare-and-swap BEFORE pushing anything. Exactly one instance
pushes a given email, and any ambiguity pushes nothing. Replayed mail is treated as
a security problem here, not a nuisance: see
``DESIGN-multi-instance-cursor-consistency.md``.

Storage is the shared in-memory store from ``fake_cloud_store``, or the real local
backend. ``poll_once`` runs for real against a fake mailbox that honors the
``$filter``, so a wrong cursor really does return old mail.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from entrabot import mcp_server
from entrabot.storage.backend import BlobBackend
from entrabot.tools import poll_state
from entrabot.tools.email_poll import GRAPH_MESSAGES_URL
from entrabot.tools.email_poll import poll_once as real_poll_once
from entrabot.tools.poll_state import (
    EMAIL_CURSOR_KEY,
    LEGACY_EMAIL_CURSOR_KEY,
    PollStateOutcome,
    initialize_email_cursor,
    read_email_cursor,
)

AGENT_UPN = "entra-agent@contoso.onmicrosoft.com"
SENDER = "alice@contoso.example"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _mail(msg_id: str, received: datetime, *, sender: str = SENDER) -> dict:
    return {
        "id": msg_id,
        "subject": f"Subject {msg_id}",
        "receivedDateTime": _iso(received),
        "bodyPreview": "preview",
        "from": {"emailAddress": {"name": sender.split("@")[0], "address": sender}},
        "hasAttachments": False,
        "conversationId": "conv-1",
    }


class FakeMailbox:
    """``GET /me/messages``: honors ``receivedDateTime gt``, newest first, ``$top`` 50."""

    def __init__(self, messages: list[dict] | None = None) -> None:
        self.messages = list(messages or [])
        self.filters: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("$filter", "")
        self.filters.append(query)
        cutoff = _parse(query.removeprefix("receivedDateTime gt ")) if query else None
        visible = [
            m for m in self.messages if cutoff is None or _parse(m["receivedDateTime"]) > cutoff
        ]
        visible.sort(key=lambda m: _parse(m["receivedDateTime"]), reverse=True)
        return httpx.Response(200, json={"value": visible[:50]})


@pytest.fixture
def mailbox():
    box = FakeMailbox()
    # Any request other than the mocked route raises instead of reaching the network.
    with respx.mock(assert_all_called=False) as router:
        router.get(GRAPH_MESSAGES_URL).mock(side_effect=box.handler)
        yield box


@pytest.fixture
def pushed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ids: list[str] = []

    async def record(msg: dict) -> None:
        ids.append(msg["id"])

    monkeypatch.setattr(mcp_server, "_push_email_notification", record)
    return ids


@pytest.fixture(autouse=True)
def _server_state(monkeypatch: pytest.MonkeyPatch):
    old_state = mcp_server._state.copy()
    old_identity = mcp_server._identity
    old_logger = mcp_server.logger
    mcp_server._state.clear()
    mcp_server._state["token"] = "fake-token"
    mcp_server._identity = None
    mcp_server.logger = logging.getLogger("entrabot.mcp_server")
    yield
    mcp_server._state.clear()
    mcp_server._state.update(old_state)
    mcp_server._identity = old_identity
    mcp_server.logger = old_logger


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "agent-data"
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(path))
    return path


async def _cycle(seen: set[str] | None = None, *, agent_upn: str = AGENT_UPN) -> None:
    await mcp_server._email_poll_cycle(
        pushed_email_ids=seen if seen is not None else set(),
        agent_self_upn=agent_upn,
    )


def _store_cursor(store, value: str) -> str:
    """Write the shared cursor directly, as another instance would."""
    etag = BlobBackend(store).write_text(EMAIL_CURSOR_KEY, value)
    assert etag
    return etag


class _ClaimOutage:
    """Storage that drops out as the cursor claim is written, and stays out until recovered.

    The first claim write raises as if its response had been lost, and every storage call
    after it fails too, so the claim can be neither confirmed nor refuted. ``landed`` says
    whether that write had committed first, which nothing on the client's side can tell.
    ``recover()`` brings storage back, after which everything works as usual.

    ``recover(except_plain_reads=True)`` brings back everything but plain reads, which is
    what settling a claim uses. A cycle that wrongly carried on past an unsettled claim
    would then read the cursor, poll Graph and claim again, instead of being stopped by
    the outage, so the test can see it.
    """

    def __init__(self, store, monkeypatch: pytest.MonkeyPatch, *, landed: bool) -> None:
        self.down = False
        self.plain_reads_down = False
        self.armed = True
        self.claim_writes = 0
        real_get, real_get_with_etag, real_put = store.get, store.get_with_etag, store.put

        def check_up(*, plain_read: bool = False) -> None:
            if self.down or (plain_read and self.plain_reads_down):
                raise OSError("storage outage")

        async def get(path):
            check_up(plain_read=True)
            return await real_get(path)

        async def get_with_etag(path):
            check_up()
            return await real_get_with_etag(path)

        async def put(path, data, **kwargs):
            check_up()
            is_claim = path == EMAIL_CURSOR_KEY and kwargs.get("if_match") is not None
            if is_claim:
                self.claim_writes += 1
            if is_claim and self.armed:
                self.armed = False
                if landed:
                    await real_put(path, data, **kwargs)
                self.down = True
                raise OSError("connection reset")
            return await real_put(path, data, **kwargs)

        monkeypatch.setattr(store, "get", get)
        monkeypatch.setattr(store, "get_with_etag", get_with_etag)
        monkeypatch.setattr(store, "put", put)

    def recover(self, *, except_plain_reads: bool = False) -> None:
        self.down = False
        self.plain_reads_down = except_plain_reads


# ---------------------------------------------------------------------------
# The near-miss: stale state must not replay
# ---------------------------------------------------------------------------
class TestStaleStateDoesNotReplay:
    async def test_stale_old_cloud_key_is_ignored_on_first_cycle(
        self, fake_cloud_store, mailbox, pushed, blob_calls_on_event_loop
    ) -> None:
        """setup's migration left a months-old ``email_cursor.txt`` in the container.
        Reading it would push up to 50 existing emails as new on upgrade."""
        months_ago = datetime.now(UTC) - timedelta(days=120)
        BlobBackend(fake_cloud_store).write_text(LEGACY_EMAIL_CURSOR_KEY, _iso(months_ago))
        blob_calls_on_event_loop.clear()  # that seed write isn't the code under test
        mailbox.messages = [
            _mail(f"old-{i}", months_ago + timedelta(days=1, minutes=i)) for i in range(60)
        ]

        await _cycle()
        assert pushed == []
        await _cycle()
        assert pushed == []
        assert blob_calls_on_event_loop == []

        cursor = _parse(read_email_cursor().cursor or "")
        assert cursor > datetime.now(UTC) - timedelta(minutes=5)

    async def test_stale_local_cursor_is_ignored_and_never_replays(
        self, fake_cloud_store, mailbox, pushed, data_dir: Path
    ) -> None:
        """A machine switched to another agent's container keeps its old local file."""
        months_ago = datetime.now(UTC) - timedelta(days=120)
        data_dir.mkdir(parents=True)
        (data_dir / "email_cursor.txt").write_text(_iso(months_ago))
        mailbox.messages = [
            _mail(f"old-{i}", months_ago + timedelta(days=1, minutes=i)) for i in range(60)
        ]

        await _cycle()
        await _cycle()

        assert pushed == []
        assert (data_dir / "email_cursor.txt").read_text() == _iso(months_ago)

    async def test_mail_after_first_boot_is_pushed(self, fake_cloud_store, mailbox, pushed) -> None:
        await _cycle()
        mailbox.messages = [_mail("new", datetime.now(UTC) + timedelta(minutes=1))]
        await _cycle()
        assert pushed == ["new"]

    async def test_cloud_mode_creates_no_local_files(
        self, fake_cloud_store, mailbox, pushed, data_dir: Path
    ) -> None:
        await _cycle()
        mailbox.messages = [_mail("new", datetime.now(UTC) + timedelta(minutes=1))]
        await _cycle()
        assert pushed == ["new"]
        assert not data_dir.exists() or list(data_dir.rglob("*")) == []


# ---------------------------------------------------------------------------
# Two instances, one mailbox
# ---------------------------------------------------------------------------
class TestTwoInstances:
    async def test_sequential_cycles_push_each_email_once(
        self, fake_cloud_store, mailbox, pushed
    ) -> None:
        await _cycle()
        mailbox.messages = [_mail("m1", datetime.now(UTC) + timedelta(minutes=1))]

        await _cycle(set())  # instance A
        await _cycle(set())  # instance B: reads the advanced cursor, Graph has nothing

        assert pushed == ["m1"]

    async def test_racing_cycles_exactly_one_pushes(
        self, fake_cloud_store, pushed, monkeypatch, blob_calls_on_event_loop
    ) -> None:
        """Both read the same cursor and ETag and fetch the same batch before either
        claims. Only the compare-and-swap decides who owns it."""
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        blob_calls_on_event_loop.clear()  # that seed write isn't the code under test
        both_fetched = asyncio.Barrier(2)
        batch = [
            _mail("m1", _parse("2026-11-02T12:01:00Z")),
            _mail("m2", _parse("2026-11-02T12:02:00Z")),
        ]

        async def fetch(*, token: str, cursor: str | None) -> tuple[list[dict], str]:
            assert cursor == "2026-11-02T12:00:00Z"
            await both_fetched.wait()
            return batch, "2026-11-02T12:02:00.001Z"

        monkeypatch.setattr("entrabot.tools.email_poll.poll_once", fetch)

        await asyncio.gather(_cycle(set()), _cycle(set()))

        assert blob_calls_on_event_loop == []
        assert sorted(pushed) == ["m1", "m2"], "the winner pushes each email, the loser nothing"
        assert read_email_cursor().cursor == "2026-11-02T12:02:00.001Z"

    async def test_loser_logs_at_info_and_drops_its_batch(
        self, fake_cloud_store, pushed, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="entrabot.mcp_server")
        etag = _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")

        async def fetch_then_lose(*, token: str, cursor: str | None) -> tuple[list[dict], str]:
            # Another instance claims this window while this one is fetching.
            BlobBackend(fake_cloud_store).write_text(
                EMAIL_CURSOR_KEY, "2026-11-02T12:09:00Z", if_match=etag
            )
            return [_mail("m1", _parse("2026-11-02T12:01:00Z"))], "2026-11-02T12:01:00.001Z"

        monkeypatch.setattr("entrabot.tools.email_poll.poll_once", fetch_then_lose)

        await _cycle()

        assert pushed == []
        assert read_email_cursor().cursor == "2026-11-02T12:09:00Z", "the winner's cursor stands"
        records = [r for r in caplog.records if "claimed" in r.getMessage()]
        assert records and all(r.levelno == logging.INFO for r in records)

    async def test_mail_the_loser_saw_first_is_still_delivered_later(
        self, fake_cloud_store, mailbox, pushed, monkeypatch
    ) -> None:
        """Losing a race drops the batch, not the mail. A saw only m1 and won; B saw
        m1 and m2 and lost. m2 is past A's cursor, so a later cycle still gets it."""
        start = "2026-11-02T12:00:00Z"
        m1 = _mail("m1", _parse("2026-11-02T12:01:00Z"))
        m2 = _mail("m2", _parse("2026-11-02T12:02:00Z"))
        start_etag = _store_cursor(fake_cloud_store, start)
        both_fetched = asyncio.Barrier(2)
        batches = iter(
            [
                ([m1], "2026-11-02T12:01:00.001Z"),
                ([m1, m2], "2026-11-02T12:02:00.001Z"),
            ]
        )

        async def fetch(*, token: str, cursor: str | None) -> tuple[list[dict], str]:
            batch = next(batches)
            await both_fetched.wait()
            if batch[0] == [m1, m2]:
                # B returns only once A's claim has landed, so A wins every time.
                while fake_cloud_store.etags[EMAIL_CURSOR_KEY] == start_etag:
                    await asyncio.sleep(0.01)
            return batch

        monkeypatch.setattr("entrabot.tools.email_poll.poll_once", fetch)
        await asyncio.gather(_cycle(set()), _cycle(set()))
        assert pushed == ["m1"]

        monkeypatch.setattr("entrabot.tools.email_poll.poll_once", real_poll_once)
        mailbox.messages = [m1, m2]
        await _cycle(set())
        assert pushed == ["m1", "m2"]


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------
class TestFailClosed:
    async def test_unresolved_read_pushes_nothing_writes_nothing_and_retries(
        self, fake_cloud_store, mailbox, pushed, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        start = "2026-11-02T12:00:00Z"
        etag = _store_cursor(fake_cloud_store, start)
        mailbox.messages = [_mail("m1", _parse("2026-11-02T12:01:00Z"))]

        with patch.object(
            fake_cloud_store, "get_with_etag", AsyncMock(side_effect=OSError("outage"))
        ):
            await _cycle()

        assert pushed == []
        assert mailbox.filters == [], "must not even poll Graph without a resolved cursor"
        assert fake_cloud_store.etags[EMAIL_CURSOR_KEY] == etag, "no cursor written"
        assert "unresolved" in caplog.text.lower()

        await _cycle()
        assert pushed == ["m1"], "the next cycle retries and delivers"

    async def test_unresolved_read_never_initializes_to_now(
        self, fake_cloud_store, mailbox, pushed
    ) -> None:
        """A failed read could be hiding a live cursor. Starting at now would skip
        mail, and on a real absent key the read would have said ABSENT."""
        with patch.object(
            fake_cloud_store, "get_with_etag", AsyncMock(side_effect=OSError("outage"))
        ):
            await _cycle()

        assert EMAIL_CURSOR_KEY not in fake_cloud_store.data

    async def test_unresolved_read_never_falls_back_to_local(
        self, fake_cloud_store, mailbox, pushed, data_dir: Path
    ) -> None:
        months_ago = datetime.now(UTC) - timedelta(days=120)
        data_dir.mkdir(parents=True)
        (data_dir / "email_cursor.txt").write_text(_iso(months_ago))
        mailbox.messages = [_mail("old", months_ago + timedelta(days=1))]

        with patch.object(
            fake_cloud_store, "get_with_etag", AsyncMock(side_effect=OSError("outage"))
        ):
            await _cycle()

        assert pushed == []
        assert mailbox.filters == []

    async def test_corrupt_cursor_pushes_nothing_and_does_not_reinitialize(
        self, fake_cloud_store, mailbox, pushed
    ) -> None:
        BlobBackend(fake_cloud_store).write_text(EMAIL_CURSOR_KEY, "<html>proxy</html>")
        mailbox.messages = [_mail("m1", datetime.now(UTC))]

        await _cycle()

        assert pushed == []
        assert mailbox.filters == []
        assert fake_cloud_store.data[EMAIL_CURSOR_KEY].decode() == "<html>proxy</html>"

    async def test_non_conflict_write_failure_pushes_nothing_then_retries(
        self, fake_cloud_store, mailbox, pushed
    ) -> None:
        start = "2026-11-02T12:00:00Z"
        _store_cursor(fake_cloud_store, start)
        mailbox.messages = [_mail("m1", _parse("2026-11-02T12:01:00Z"))]

        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            await _cycle()

        assert pushed == [], "a push the fleet can't see claimed could be repeated"
        assert read_email_cursor().cursor == start

        await _cycle()
        assert pushed == ["m1"]

    async def test_graph_failure_pushes_nothing_and_moves_nothing(
        self, fake_cloud_store, pushed
    ) -> None:
        start = "2026-11-02T12:00:00Z"
        _store_cursor(fake_cloud_store, start)
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(return_value=httpx.Response(500))
            with pytest.raises(httpx.HTTPStatusError):
                await _cycle()
        assert pushed == []
        assert read_email_cursor().cursor == start

    async def test_initialization_failure_is_retried_next_cycle(
        self, fake_cloud_store, mailbox, pushed
    ) -> None:
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            await _cycle()
        assert read_email_cursor().outcome is PollStateOutcome.ABSENT

        await _cycle()
        assert read_email_cursor().outcome is PollStateOutcome.PRESENT
        assert pushed == []


# ---------------------------------------------------------------------------
# The first start time survives failed creation
# ---------------------------------------------------------------------------
FIRST_ATTEMPT = "2026-11-02T12:00:00.000Z"
SECOND_ATTEMPT = "2026-11-02T13:00:00.000Z"


class TestFirstStartTimeSurvivesFailedCreation:
    """A cursor that can't be created for a while still starts where the agent started.

    Every attempt used to read its own "now", so an hour of failed creation moved the
    start an hour forward and skipped that hour's mail once storage recovered.
    """

    @pytest.fixture(autouse=True)
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """``now_cursor`` as an hour hand: each read is an hour after the one before."""
        reads: list[str] = []

        def tick() -> str:
            reads.append(f"2026-11-02T{12 + len(reads):02d}:00:00.000Z")
            return reads[-1]

        monkeypatch.setattr(poll_state, "now_cursor", tick)
        return reads

    async def test_the_cursor_starts_at_the_first_attempt_not_the_one_that_succeeds(
        self, fake_cloud_store
    ) -> None:
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            await mcp_server._email_cursor_or_initialize()
            await mcp_server._email_cursor_or_initialize()
        assert read_email_cursor().outcome is PollStateOutcome.ABSENT

        await mcp_server._email_cursor_or_initialize()  # storage is back

        assert read_email_cursor().cursor == FIRST_ATTEMPT

    async def test_the_held_start_is_cleared_once_the_cursor_is_created(
        self, fake_cloud_store
    ) -> None:
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            await mcp_server._email_cursor_or_initialize()
        assert mcp_server._state["_email_cursor_start"] == FIRST_ATTEMPT

        await mcp_server._email_cursor_or_initialize()

        assert "_email_cursor_start" not in mcp_server._state

    async def test_a_present_read_clears_the_held_start_so_a_later_deletion_starts_fresh(
        self, fake_cloud_store
    ) -> None:
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            await mcp_server._email_cursor_or_initialize()
        assert mcp_server._state["_email_cursor_start"] == FIRST_ATTEMPT
        _store_cursor(fake_cloud_store, "2026-11-02T12:10:00Z")  # another instance created it

        current = await mcp_server._email_cursor_or_initialize()

        assert current.outcome is PollStateOutcome.PRESENT
        assert "_email_cursor_start" not in mcp_server._state

        # Someone deletes the key. The new cursor starts from the time of this attempt,
        # not from the old held one.
        del fake_cloud_store.data[EMAIL_CURSOR_KEY]
        del fake_cloud_store.etags[EMAIL_CURSOR_KEY]
        await mcp_server._email_cursor_or_initialize()
        assert read_email_cursor().cursor == SECOND_ATTEMPT

    async def test_an_unresolved_read_neither_creates_the_cursor_nor_drops_the_held_start(
        self, fake_cloud_store
    ) -> None:
        with patch.object(fake_cloud_store, "put", AsyncMock(side_effect=OSError("outage"))):
            await mcp_server._email_cursor_or_initialize()
        with patch.object(
            fake_cloud_store, "get_with_etag", AsyncMock(side_effect=OSError("outage"))
        ):
            await mcp_server._email_cursor_or_initialize()
        assert mcp_server._state["_email_cursor_start"] == FIRST_ATTEMPT

        await mcp_server._email_cursor_or_initialize()

        assert read_email_cursor().cursor == FIRST_ATTEMPT

    @pytest.mark.parametrize("created", [True, False])
    async def test_the_start_is_handed_on_and_dropped_only_when_creation_reports_success(
        self, monkeypatch: pytest.MonkeyPatch, created: bool
    ) -> None:
        """True also covers losing the create race to another instance, which keeps its value."""
        monkeypatch.setattr(
            poll_state,
            "read_email_cursor",
            lambda: poll_state.EmailCursorRead(PollStateOutcome.ABSENT),
        )
        handed_on: list[str | None] = []

        def initialize(now: str | None = None) -> bool:
            handed_on.append(now)
            return created

        monkeypatch.setattr(poll_state, "initialize_email_cursor", initialize)

        await mcp_server._email_cursor_or_initialize()
        await mcp_server._email_cursor_or_initialize()

        if created:
            assert handed_on == [FIRST_ATTEMPT, SECOND_ATTEMPT]
            assert "_email_cursor_start" not in mcp_server._state
        else:
            assert handed_on == [FIRST_ATTEMPT, FIRST_ATTEMPT]
            assert mcp_server._state["_email_cursor_start"] == FIRST_ATTEMPT


# ---------------------------------------------------------------------------
# An ambiguous claim holds its batch instead of dropping it
# ---------------------------------------------------------------------------
START = "2026-11-02T12:00:00Z"
AFTER_M1 = "2026-11-02T12:01:00.001Z"


def _server_warnings(caplog: pytest.LogCaptureFixture, text: str) -> list[logging.LogRecord]:
    """Warnings the poll cycle itself logged, as opposed to the storage layer under it."""
    return [
        record
        for record in caplog.records
        if record.name == "entrabot.mcp_server"
        and record.levelno == logging.WARNING
        and text in record.getMessage()
    ]


class TestAmbiguousClaim:
    """A claim write that fails and can't be checked may still have moved the cursor.

    If it did, no later poll will fetch that mail, so the batch is held in memory and the
    claim is settled once storage answers: pushed if it landed, fetched again if it
    didn't. Nothing is pushed, polled or written in the meantime.
    """

    @pytest.fixture
    def m1(self, mailbox) -> dict:
        message = _mail("m1", _parse("2026-11-02T12:01:00Z"))
        mailbox.messages = [message]
        return message

    async def test_an_unknown_claim_holds_the_batch_and_pushes_nothing(
        self, fake_cloud_store, mailbox, pushed, monkeypatch, m1, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        _store_cursor(fake_cloud_store, START)
        _ClaimOutage(fake_cloud_store, monkeypatch, landed=True)

        await _cycle()

        assert pushed == []
        held = mcp_server._state["_email_pending_claim"]
        assert isinstance(held, mcp_server.PendingEmailClaim)
        assert [message["id"] for message in held.messages] == ["m1"]
        assert held.claim_id
        assert _server_warnings(caplog, "holding"), "the cycle itself says it is holding"

    @pytest.mark.parametrize("landed", [True, False])
    async def test_the_next_cycle_polls_nothing_and_writes_nothing_while_still_unknown(
        self,
        fake_cloud_store,
        mailbox,
        pushed,
        monkeypatch,
        m1,
        landed: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        _store_cursor(fake_cloud_store, START)
        outage = _ClaimOutage(fake_cloud_store, monkeypatch, landed=landed)
        await _cycle()
        held = mcp_server._state["_email_pending_claim"]
        # Only the read that settles the claim keeps failing. Everything else works, so
        # a cycle that carried on regardless could read the cursor, poll Graph and claim
        # again, and the outage wouldn't stop it.
        outage.recover(except_plain_reads=True)
        caplog.clear()

        await _cycle()

        assert len(mailbox.filters) == 1, "no Graph poll while the claim is unsettled"
        assert outage.claim_writes == 1, "no second cursor write"
        assert pushed == []
        assert mcp_server._state["_email_pending_claim"] is held, "still held, unchanged"
        assert _server_warnings(caplog, "holding"), "and it says so again"

    async def test_a_claim_that_turns_out_to_have_landed_pushes_the_held_batch_once(
        self, fake_cloud_store, mailbox, pushed, monkeypatch, m1, blob_calls_on_event_loop
    ) -> None:
        _store_cursor(fake_cloud_store, START)
        outage = _ClaimOutage(fake_cloud_store, monkeypatch, landed=True)
        await _cycle()
        await _cycle()  # still down
        outage.recover()
        blob_calls_on_event_loop.clear()  # the seed write and the outage aren't under test

        await _cycle()

        assert pushed == ["m1"]
        assert "_email_pending_claim" not in mcp_server._state
        assert len(mailbox.filters) == 1, "settling the claim polls nothing in that cycle"
        assert blob_calls_on_event_loop == [], "settling is storage I/O and stays off the loop"

        await _cycle()  # the next ordinary cycle starts from the advanced cursor
        await _cycle()
        assert pushed == ["m1"], "pushed exactly once"
        assert mailbox.filters[1:] == [f"receivedDateTime gt {AFTER_M1}"] * 2

    async def test_a_claim_that_did_not_land_clears_and_polls_normally(
        self, fake_cloud_store, mailbox, pushed, monkeypatch, m1
    ) -> None:
        _store_cursor(fake_cloud_store, START)
        outage = _ClaimOutage(fake_cloud_store, monkeypatch, landed=False)
        await _cycle()
        assert mcp_server._state["_email_pending_claim"].messages == [m1]
        outage.recover()

        await _cycle()

        assert "_email_pending_claim" not in mcp_server._state
        assert mailbox.filters == [f"receivedDateTime gt {START}"] * 2, "it polled again"
        assert pushed == ["m1"], "the normal fetch got the same mail and claimed it afresh"
        assert read_email_cursor().cursor == AFTER_M1

    async def test_a_held_claim_is_found_behind_a_siblings_newer_claim(
        self, fake_cloud_store, mailbox, pushed, monkeypatch, m1
    ) -> None:
        """Our write landed, and another instance has claimed the next window since.
        Its write carries our claim ID forward, so the batch is still ours to push."""
        _store_cursor(fake_cloud_store, START)
        outage = _ClaimOutage(fake_cloud_store, monkeypatch, landed=True)
        await _cycle()
        ours = fake_cloud_store.data[EMAIL_CURSOR_KEY].decode().splitlines()[1:]
        fake_cloud_store.data[EMAIL_CURSOR_KEY] = "\n".join(
            ["2026-11-02T12:09:00.001Z", "sibling-claim", *ours]
        ).encode()
        outage.recover()

        await _cycle()

        assert pushed == ["m1"]
        assert "_email_pending_claim" not in mcp_server._state

    async def test_the_held_batch_still_skips_the_agents_own_mail(
        self, fake_cloud_store, mailbox, pushed, monkeypatch
    ) -> None:
        mailbox.messages = [
            _mail("self", _parse("2026-11-02T12:01:00Z"), sender=AGENT_UPN),
            _mail("m1", _parse("2026-11-02T12:02:00Z")),
        ]
        _store_cursor(fake_cloud_store, START)
        outage = _ClaimOutage(fake_cloud_store, monkeypatch, landed=True)
        seen: set[str] = set()
        await _cycle(seen)
        outage.recover()

        await _cycle(seen)

        assert pushed == ["m1"]
        assert seen == {"self", "m1"}

    async def test_a_won_claim_carries_the_history_forward(
        self, fake_cloud_store, mailbox, pushed, m1
    ) -> None:
        _store_cursor(fake_cloud_store, f"{START}\nsibling-2\nsibling-1")

        await _cycle()

        assert pushed == ["m1"]
        current = read_email_cursor()
        assert current.cursor == AFTER_M1
        assert current.recent_claims[1:] == ("sibling-2", "sibling-1")
        assert current.recent_claims[0] not in ("sibling-2", "sibling-1"), "a fresh claim ID"

    async def test_a_lost_claim_is_still_dropped_not_held(
        self, fake_cloud_store, mailbox, pushed, monkeypatch
    ) -> None:
        etag = _store_cursor(fake_cloud_store, START)

        async def fetch_then_lose(*, token: str, cursor: str | None) -> tuple[list[dict], str]:
            BlobBackend(fake_cloud_store).write_text(
                EMAIL_CURSOR_KEY, "2026-11-02T12:09:00Z", if_match=etag
            )
            return [_mail("m1", _parse("2026-11-02T12:01:00Z"))], AFTER_M1

        monkeypatch.setattr("entrabot.tools.email_poll.poll_once", fetch_then_lose)

        await _cycle()

        assert pushed == []
        assert "_email_pending_claim" not in mcp_server._state


# ---------------------------------------------------------------------------
# Behaviour kept from the old loop
# ---------------------------------------------------------------------------
class TestCycleBehaviour:
    async def test_cursor_is_reread_every_cycle(self, fake_cloud_store, mailbox, pushed) -> None:
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        await _cycle()
        _store_cursor(fake_cloud_store, "2026-11-02T13:00:00Z")  # a sibling advanced it
        await _cycle()
        assert mailbox.filters == [
            "receivedDateTime gt 2026-11-02T12:00:00Z",
            "receivedDateTime gt 2026-11-02T13:00:00Z",
        ]

    async def test_cursor_is_claimed_before_anything_is_pushed(
        self, fake_cloud_store, mailbox, monkeypatch
    ) -> None:
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        mailbox.messages = [_mail("m1", _parse("2026-11-02T12:01:00Z"))]
        cursor_at_push: list[str | None] = []

        async def record(msg: dict) -> None:
            cursor_at_push.append(read_email_cursor().cursor)

        monkeypatch.setattr(mcp_server, "_push_email_notification", record)
        await _cycle()
        assert cursor_at_push == ["2026-11-02T12:01:00.001Z"]

    async def test_all_noise_still_advances_the_cursor(
        self, fake_cloud_store, mailbox, pushed
    ) -> None:
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        mailbox.messages = [
            _mail("noise", _parse("2026-11-02T12:01:00Z"), sender="no-reply@teams.mail.microsoft")
        ]
        await _cycle()
        assert pushed == []
        assert read_email_cursor().cursor == "2026-11-02T12:01:00.001Z"

    async def test_returned_mail_at_or_before_the_cursor_is_not_claimed_or_pushed(
        self, fake_cloud_store, pushed
    ) -> None:
        """Graph's ``gt`` shouldn't return these, but if it does they are already
        behind the watermark, so there is no window to claim and nothing to push."""
        etag = _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        old = _mail("old", _parse("2026-11-02T11:00:00Z"))
        with respx.mock:
            respx.get(GRAPH_MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"value": [old]})
            )
            await _cycle()
        assert pushed == []
        assert fake_cloud_store.etags[EMAIL_CURSOR_KEY] == etag

    async def test_no_mail_writes_nothing(self, fake_cloud_store, mailbox, pushed) -> None:
        etag = _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        await _cycle()
        assert fake_cloud_store.etags[EMAIL_CURSOR_KEY] == etag

    async def test_self_sent_mail_is_not_pushed(self, fake_cloud_store, mailbox, pushed) -> None:
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        seen: set[str] = set()
        mailbox.messages = [_mail("self", _parse("2026-11-02T12:01:00Z"), sender=AGENT_UPN)]
        await _cycle(seen)
        assert pushed == []
        assert seen == {"self"}, "marked seen so dedup still covers it"

    async def test_self_filter_ignores_case(self, fake_cloud_store, mailbox, pushed) -> None:
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        mailbox.messages = [_mail("self", _parse("2026-11-02T12:01:00Z"), sender=AGENT_UPN.upper())]
        await _cycle()
        assert pushed == []

    async def test_session_dedup_blocks_a_second_push_of_the_same_id(
        self, fake_cloud_store, mailbox, pushed
    ) -> None:
        seen: set[str] = set()
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        mailbox.messages = [_mail("m1", _parse("2026-11-02T12:01:00Z"))]
        await _cycle(seen)
        # The same message comes around again; this instance has already pushed it.
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        await _cycle(seen)
        assert pushed == ["m1"]

    async def test_session_dedup_set_stays_bounded(self, fake_cloud_store, mailbox, pushed) -> None:
        seen = {f"old-{i}" for i in range(mcp_server.PUSHED_EMAIL_MAX + 1)}
        _store_cursor(fake_cloud_store, "2026-11-02T12:00:00Z")
        mailbox.messages = [_mail("m1", _parse("2026-11-02T12:01:00Z"))]
        await _cycle(seen)
        assert len(seen) <= mcp_server.PUSHED_EMAIL_MAX


class TestPushEmailBatch:
    """The push loop on its own, so the cycle and a settled claim share one copy of it."""

    async def test_pushes_each_message_in_order_and_remembers_it(self, pushed) -> None:
        seen: set[str] = set()
        batch = [
            _mail("m1", _parse("2026-11-02T12:01:00Z")),
            _mail("m2", _parse("2026-11-02T12:02:00Z")),
        ]
        await mcp_server._push_email_batch(batch, seen, AGENT_UPN)
        assert pushed == ["m1", "m2"]
        assert seen == {"m1", "m2"}

    async def test_the_agents_own_mail_is_skipped_but_marked_seen(self, pushed) -> None:
        seen: set[str] = set()
        batch = [_mail("self", _parse("2026-11-02T12:01:00Z"), sender=AGENT_UPN.upper())]
        await mcp_server._push_email_batch(batch, seen, AGENT_UPN)
        assert pushed == []
        assert seen == {"self"}

    async def test_a_message_this_session_already_pushed_is_skipped(self, pushed) -> None:
        batch = [_mail("m1", _parse("2026-11-02T12:01:00Z"))]
        await mcp_server._push_email_batch(batch, {"m1"}, AGENT_UPN)
        assert pushed == []

    async def test_the_dedup_set_stays_bounded(self, pushed) -> None:
        seen = {f"old-{i}" for i in range(mcp_server.PUSHED_EMAIL_MAX + 1)}
        batch = [_mail("m1", _parse("2026-11-02T12:01:00Z"))]
        await mcp_server._push_email_batch(batch, seen, AGENT_UPN)
        assert len(seen) <= mcp_server.PUSHED_EMAIL_MAX


# ---------------------------------------------------------------------------
# Local mode
# ---------------------------------------------------------------------------
class TestLocalMode:
    async def test_legacy_cursor_is_adopted_so_there_is_no_gap(
        self, data_dir: Path, mailbox, pushed
    ) -> None:
        """A local-only user upgrading keeps mail from while the server was down."""
        data_dir.mkdir(parents=True)
        (data_dir / "email_cursor.txt").write_text("2026-11-02T12:00:00Z")
        mailbox.messages = [_mail("while-down", _parse("2026-11-02T12:30:00Z"))]

        await _cycle()  # first cycle adopts
        await _cycle()  # next cycle polls from the adopted value

        assert mailbox.filters == ["receivedDateTime gt 2026-11-02T12:00:00Z"]
        assert pushed == ["while-down"]

    async def test_legacy_file_is_left_alone_and_new_key_takes_over(
        self, data_dir: Path, mailbox, pushed
    ) -> None:
        data_dir.mkdir(parents=True)
        (data_dir / "email_cursor.txt").write_text("2026-11-02T12:00:00Z")
        mailbox.messages = [_mail("m1", _parse("2026-11-02T12:30:00Z"))]

        await _cycle()
        await _cycle()

        assert (data_dir / "email_cursor.txt").read_text() == "2026-11-02T12:00:00Z"
        stored = (data_dir / "poll_state" / "email_cursor.txt").read_text()
        assert stored.splitlines()[0] == "2026-11-02T12:30:00.001Z"

    async def test_no_legacy_cursor_starts_at_now(self, data_dir: Path, mailbox, pushed) -> None:
        mailbox.messages = [_mail("old", datetime.now(UTC) - timedelta(days=30))]
        await _cycle()
        await _cycle()
        assert pushed == []


# ---------------------------------------------------------------------------
# The background task
# ---------------------------------------------------------------------------
class _StopLoop(BaseException):
    """Ends a ``while True`` task from a patched sleep without being swallowed."""


def _sleep_then_stop(after: int):
    calls = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] > after:
            raise _StopLoop

    return fake_sleep


class TestBackgroundTask:
    async def test_cursor_is_initialized_before_the_first_sleep(
        self, fake_cloud_store, mailbox, pushed, blob_calls_on_event_loop
    ) -> None:
        """Otherwise mail arriving in the first interval after boot goes unnoticed."""
        with (
            patch.object(asyncio, "sleep", _sleep_then_stop(after=0)),
            pytest.raises(_StopLoop),
        ):
            await mcp_server._background_poll_email()

        assert blob_calls_on_event_loop == []
        assert read_email_cursor().outcome is PollStateOutcome.PRESENT
        assert mailbox.filters == [], "first poll waits for the first interval, as before"

    async def test_mail_arriving_in_the_first_interval_is_pushed(
        self, fake_cloud_store, mailbox, pushed
    ) -> None:
        arrives = datetime.now(UTC) + timedelta(seconds=30)

        with (
            patch.object(asyncio, "sleep", _sleep_then_stop(after=1)),
            patch.object(mcp_server, "_ensure_valid_token", AsyncMock()),
            pytest.raises(_StopLoop),
        ):
            mailbox.messages = [_mail("first-minute", arrives)]
            await mcp_server._background_poll_email()

        assert pushed == ["first-minute"]

    async def test_a_failing_cycle_does_not_kill_the_loop(
        self, fake_cloud_store, pushed, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="entrabot.mcp_server")
        initialize_email_cursor("2026-11-02T12:00:00Z")
        with (
            respx.mock,
            patch.object(asyncio, "sleep", _sleep_then_stop(after=3)),
            patch.object(mcp_server, "_ensure_valid_token", AsyncMock()),
            pytest.raises(_StopLoop),
        ):
            respx.get(GRAPH_MESSAGES_URL).mock(return_value=httpx.Response(500))
            await mcp_server._background_poll_email()

        assert "Email poll error" in caplog.text
