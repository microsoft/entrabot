"""Tests for the entrabot logging configuration."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from entrabot.logging_config import setup_logging


class TestSetupLogging:
    def _reset(self) -> None:
        logger = logging.getLogger("entrabot")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logger.propagate = True  # restore default before each test

    def test_does_not_propagate_to_root(self) -> None:
        """Prevent double-logging when FastMCP attaches a RichHandler to root.

        FastMCP's ``configure_logging`` calls ``logging.basicConfig(...)`` which
        attaches a ``RichHandler`` to the root logger. Without this guard, every
        record on the ``entrabot`` logger propagates to root and gets written a
        second time in rich format — doubling stderr volume that the parent
        Claude Code CLI has to drain. ``setup_logging`` must mark the logger
        non-propagating.
        """
        self._reset()

        setup_logging()

        assert logging.getLogger("entrabot").propagate is False

    def test_root_handler_does_not_see_entrabot_records(self) -> None:
        """End-to-end check: a handler on root must not receive entrabot records."""
        self._reset()

        captured: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        root = logging.getLogger()
        sink = Capture()
        root.addHandler(sink)
        try:
            setup_logging()
            logging.getLogger("entrabot").info("propagation-check")
            logging.getLogger("entrabot.tools.teams").info("child-propagation-check")
        finally:
            root.removeHandler(sink)

        assert captured == [], (
            f"root logger captured {len(captured)} entrabot record(s); "
            "propagation should be blocked at the entrabot logger"
        )

    def test_file_handler_rotates_to_cap_disk_usage(self, tmp_path, monkeypatch) -> None:
        """The MCP server log must not grow forever on disk."""
        self._reset()
        monkeypatch.setenv("ENTRABOT_LOG_DIR", str(tmp_path))

        logger = setup_logging()

        file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1
        handler = file_handlers[0]
        assert handler.baseFilename == str(tmp_path / "entrabot.log")
        assert handler.maxBytes > 0
        assert handler.backupCount > 0
