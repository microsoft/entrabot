"""JSONL-based IPC handler for MCP server ↔ Bot server communication.

Two JSONL files under ~/.entraclaw/bot/:
  - inbound.jsonl  — Bot writes, MCP server reads (messages from Teams)
  - outbound.jsonl — MCP server writes, Bot reads (messages to send to Teams)

Each line is a single JSON object. Reads are destructive (truncate after read).
Advisory file locking via fcntl.flock prevents concurrent corruption.
"""

from __future__ import annotations

import fcntl
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

BOT_DIR = Path.home() / ".entraclaw" / "bot"


def _bot_dir() -> Path:
    """Ensure the bot IPC directory exists and return its path."""
    BOT_DIR.mkdir(parents=True, exist_ok=True)
    return BOT_DIR


def _write_jsonl(filename: str, message: dict) -> None:
    """Append a single JSON line to a JSONL file under BOT_DIR."""
    path = _bot_dir() / filename
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(message) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read_jsonl(filename: str) -> list[dict]:
    """Read and consume all messages from a JSONL file under BOT_DIR.

    Returns parsed messages. Corrupted lines are skipped with a warning.
    The file is truncated after reading so messages are consumed exactly once.
    """
    path = _bot_dir() / filename
    if not path.exists():
        return []

    messages: list[dict] = []
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupted JSONL line in %s: %s", filename, line)
            f.truncate(0)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    return messages


# --- Public API ---


def write_inbound(message: dict) -> None:
    """Append an inbound message (from Teams) to inbound.jsonl."""
    _write_jsonl("inbound.jsonl", message)


def read_inbound() -> list[dict]:
    """Read and consume all messages from inbound.jsonl."""
    return _read_jsonl("inbound.jsonl")


def write_outbound(message: dict) -> None:
    """Append an outbound message (to Teams) to outbound.jsonl."""
    _write_jsonl("outbound.jsonl", message)


def read_outbound() -> list[dict]:
    """Read and consume all messages from outbound.jsonl."""
    return _read_jsonl("outbound.jsonl")
