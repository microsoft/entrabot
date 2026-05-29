"""JSON-structured logging for EntraBot.

Logs to ~/.entrabot/logs/entrabot.log with a JSON formatter.
Log level is controlled by the ENTRABOT_LOG_LEVEL env var (default: INFO).
Directories are created lazily on first use.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from entrabot.config import get_config

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        # Merge any extra fields attached by callers
        for key in ("agent_id", "action", "resource", "event_id"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry)


def setup_logging() -> logging.Logger:
    """Configure the root ``entrabot`` logger and return it."""
    cfg = get_config()
    logger = logging.getLogger("entrabot")

    if logger.handlers:
        return logger  # already configured

    logger.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))

    # FastMCP's configure_logging attaches a RichHandler to the root logger via
    # basicConfig. Without this, every entrabot record would propagate to root
    # and surface as a rich-formatted stderr line on top of our JSON output,
    # doubling the volume the parent MCP client has to drain.
    logger.propagate = False

    formatter = _JSONFormatter()

    # File handler — create log directory lazily
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            cfg.log_dir / "entrabot.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # Fall back to stderr if we can't write the log file
        pass

    # Stderr handler for MCP server diagnostics
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    return logger
