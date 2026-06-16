"""Shared pytest fixtures for the entrabot test suite."""

from __future__ import annotations

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
def _isolate_memory_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the memory backend from ambient cloud configuration.

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
    """
    monkeypatch.delenv("ENTRABOT_BLOB_ENDPOINT", raising=False)
    monkeypatch.delenv("ENTRABOT_BLOB_CONTAINER", raising=False)


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
