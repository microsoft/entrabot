"""Shared pytest fixtures for the entrabot test suite."""

from __future__ import annotations

import logging

import pytest


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
