"""Conversation reference persistence for proactive messaging.

Stores conversation references as JSON at ~/.entrabot/bot/conversation_refs.json,
keyed by conversation ID. Loaded on bot startup, updated on every activity.
"""

from __future__ import annotations

import fcntl
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("entrabot.bot.convo_store")

CONVO_REFS_PATH = Path.home() / ".entrabot" / "bot" / "conversation_refs.json"


def _lock_path() -> Path:
    return CONVO_REFS_PATH.with_suffix(".json.lock")


def _read_refs() -> dict[str, dict[str, Any]]:
    """Read the refs file, returning empty dict if missing or corrupted."""
    if not CONVO_REFS_PATH.exists():
        return {}
    try:
        return json.loads(CONVO_REFS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "Corrupted conversation refs file at %s — returning empty. "
            "File preserved for inspection.",
            CONVO_REFS_PATH,
        )
        return {}


def _write_refs(refs: dict[str, dict[str, Any]]) -> None:
    """Write refs dict to disk, creating parent dirs if needed."""
    CONVO_REFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONVO_REFS_PATH.write_text(json.dumps(refs, indent=2, default=str), encoding="utf-8")


def save_reference(conversation_id: str, reference: dict[str, Any]) -> None:
    """Save or update a conversation reference.

    Loads existing refs, updates the entry for conversation_id, writes back.
    Creates parent directories if needed. Thread-safe via flock.
    """
    CONVO_REFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path()
    lock.touch(exist_ok=True)
    with open(lock) as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            refs = _read_refs()
            refs[conversation_id] = reference
            _write_refs(refs)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load_reference(conversation_id: str) -> dict[str, Any] | None:
    """Load a single conversation reference by ID. Returns None if not found."""
    refs = _read_refs()
    return refs.get(conversation_id)


def load_all_references() -> dict[str, dict[str, Any]]:
    """Load all conversation references. Returns empty dict if file missing or corrupted."""
    return _read_refs()


def delete_reference(conversation_id: str) -> None:
    """Remove a conversation reference. No-op if ID doesn't exist."""
    if not CONVO_REFS_PATH.exists():
        return
    lock = _lock_path()
    lock.touch(exist_ok=True)
    with open(lock) as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            refs = _read_refs()
            refs.pop(conversation_id, None)
            _write_refs(refs)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
