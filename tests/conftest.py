"""Shared pytest fixtures for the entrabot test suite."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def audit_identity_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give tests a real config-resolved audit identity and isolated audit dir."""
    monkeypatch.setenv("ENTRABOT_AGENT_ID", os.environ.get("ENTRABOT_AGENT_ID", "test-agent-id"))
    if "ENTRABOT_AUDIT_DIR" not in os.environ:
        monkeypatch.setenv("ENTRABOT_AUDIT_DIR", str(tmp_path / "audit"))


@pytest.fixture(autouse=True)
def _isolate_memory_backend_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate the memory backend from ambient configuration and real data.

    ``storage.get_backend()`` resolves to a :class:`BlobBackend` whenever
    ``ENTRABOT_BLOB_ENDPOINT`` and ``ENTRABOT_BLOB_CONTAINER`` are both set
    (ADR-005). A developer with a live ``.env`` (or exported cloud-memory
    vars) would therefore have the whole suite silently try to reach Azure
    Blob instead of the local backend — flipping interaction-log / daily-
    summary / body-bootstrap tests from local writes to live network calls.

    Clear those two vars for every test so the default backend is the local
    one, matching CI (which has no ``.env``). Tests that specifically
    exercise blob selection set the vars themselves in-body via the same
    function-scoped ``monkeypatch`` instance, so their explicit setup runs
    after this teardown-safe clear and still wins.

    Point ``ENTRABOT_DATA_DIR`` at the test's own temp directory for the same
    reason: without it, any test that logs through the local backend appends
    to the developer's real ``~/.entrabot/data``, and setup's memory migration
    can later upload those rows to the live agent's container.
    """
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)
    monkeypatch.setenv("ENTRABOT_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def fake_cloud_store(monkeypatch: pytest.MonkeyPatch):
    """Put the process in cloud-memory mode against an in-memory blob store.

    ``get_backend()`` takes its real path to a real :class:`BlobBackend`, so the
    mode selection under test is the production one. Only ``BlobStore`` is
    swapped, for a fake with real ETag behaviour, so nothing reaches Azure and
    no storage token is ever acquired. Sharing the returned store between calls
    is how a test models several instances on one container.
    """
    from tests.storage.test_backend import _FakeBlobStore

    store = _FakeBlobStore()
    monkeypatch.setenv("ENTRABOT_BLOB_ENDPOINT", "https://fake-account.invalid")
    monkeypatch.setenv("ENTRABOT_BLOB_CONTAINER", "fake-container")
    monkeypatch.delenv("ENTRABOT_KEEP_MEMORY_LOCAL", raising=False)
    monkeypatch.setattr("entrabot.storage.backend.BlobStore", lambda **_: store)
    return store


@pytest.fixture
def blob_calls_on_event_loop(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``BlobBackend`` call that blocks a running event loop.

    ``_run_sync`` hands off to a helper thread when a loop is running but then
    waits on it, so a blob call made from a coroutine still stalls the loop. A
    blocking read like this once starved the MCP ``initialize`` handshake past the
    host's timeout. Assert this list is empty to prove the I/O stayed off the loop.
    """
    from entrabot.storage import backend as backend_module

    calls: list[str] = []
    real_run_sync = backend_module._run_sync

    def recording_run_sync(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            calls.append(coro.__qualname__)
        return real_run_sync(coro)

    monkeypatch.setattr(backend_module, "_run_sync", recording_run_sync)
    return calls


@pytest.fixture(autouse=True)
def reset_active_identity_state() -> Iterator[None]:
    """Keep the process-wide identity accessor isolated between tests."""
    from entrabot.identity import set_active_identity_state

    set_active_identity_state(None)
    yield
    set_active_identity_state(None)


@pytest.fixture(autouse=True)
def _attach_caplog_to_entrabot(caplog: pytest.LogCaptureFixture) -> None:
    """Let pytest's caplog capture records from the entrabot logger.

    ``entrabot.logging_config.setup_logging`` sets ``propagate = False`` on the
    ``entrabot`` logger so records don't surface through FastMCP's rich handler
    on root (which doubles stderr volume). caplog's handler is attached to root
    by default, so with propagation blocked it would never see entrabot
    records. Attaching caplog's handler directly to the entrabot logger for
    the duration of each test restores the expected capture behavior without
    re-enabling production propagation.
    """
    entrabot_logger = logging.getLogger("entrabot")
    entrabot_logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        entrabot_logger.removeHandler(caplog.handler)


@pytest.fixture(autouse=True)
def _clear_storage_token_cache() -> Iterator[None]:
    """The storage-token cache (#128) is process-global; never leak a token
    cached by one test into another that expects a fresh three-hop exchange."""
    from entrabot.tools.teams import clear_storage_token_cache

    clear_storage_token_cache()
    yield
    clear_storage_token_cache()
