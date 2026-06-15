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
