"""JSON-structured logging for EntraClaw.

Logs to ~/.entraclaw/logs/entraclaw.log with a JSON formatter.
Log level is controlled by the ENTRACLAW_LOG_LEVEL env var (default: INFO).
Directories are created lazily on first use.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from entraclaw.config import get_config


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
    """Configure the root ``entraclaw`` logger and return it."""
    cfg = get_config()
    logger = logging.getLogger("entraclaw")

    if logger.handlers:
        return logger  # already configured

    logger.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))

    formatter = _JSONFormatter()

    # File handler — create log directory lazily
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(cfg.log_dir / "entraclaw.log")
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
