"""Access control for inbound Teams messages — pairing + allowlist.

Mirrors the iMessage channel pattern: unknown senders get a pairing code
sent back via Teams. The terminal user approves via an MCP tool, which
adds the sender to the allowlist. Subsequent messages from that sender
are delivered to Claude Code.

State persisted in ~/.entraclaw/access.json.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("entraclaw.access")

STATE_DIR = Path.home() / ".entraclaw"
ACCESS_FILE = STATE_DIR / "access.json"

# Pairing codes expire after 1 hour
PAIRING_EXPIRY_SECONDS = 3600
# Max pending pairings at once (prevent spam)
MAX_PENDING = 5


@dataclass
class PendingPairing:
    sender_name: str
    sender_id: str  # display name from Teams message
    chat_id: str
    code: str
    created_at: float
    expires_at: float
    reply_count: int = 1


@dataclass
class AccessState:
    """Access control state for the Teams channel."""

    # List of display names allowed to send instructions
    allow_from: list[str] = field(default_factory=list)
    # Pending pairing requests: code -> PendingPairing
    pending: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "allow_from": self.allow_from,
            "pending": self.pending,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AccessState:
        return cls(
            allow_from=data.get("allow_from", []),
            pending=data.get("pending", {}),
        )


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_access() -> AccessState:
    """Load access state from disk."""
    try:
        raw = ACCESS_FILE.read_text()
        return AccessState.from_dict(json.loads(raw))
    except (FileNotFoundError, json.JSONDecodeError):
        return AccessState()


def save_access(state: AccessState) -> None:
    """Save access state to disk."""
    _ensure_state_dir()
    ACCESS_FILE.write_text(json.dumps(state.to_dict(), indent=2) + "\n")


def _prune_expired(state: AccessState) -> bool:
    """Remove expired pending pairings. Returns True if any were pruned."""
    now = time.time()
    expired = [
        code
        for code, p in state.pending.items()
        if p.get("expires_at", 0) < now
    ]
    for code in expired:
        del state.pending[code]
    return len(expired) > 0


def gate(sender_name: str, chat_id: str) -> str:
    """Check if a sender is allowed. Returns action: 'deliver', 'drop', or 'pair:<code>'.

    - 'deliver': sender is on the allowlist, message should be pushed
    - 'drop': sender is unknown and max pending reached, or already paired (resend limit)
    - 'pair:<code>': sender needs pairing, send them this code in Teams
    """
    state = load_access()
    pruned = _prune_expired(state)

    # Check allowlist
    if sender_name in state.allow_from:
        if pruned:
            save_access(state)
        return "deliver"

    # Check if already pending
    for code, p in state.pending.items():
        if p.get("sender_name") == sender_name:
            if p.get("reply_count", 1) >= 2:
                if pruned:
                    save_access(state)
                return "drop"
            p["reply_count"] = p.get("reply_count", 1) + 1
            save_access(state)
            return f"pair:{code}"

    # Too many pending
    if len(state.pending) >= MAX_PENDING:
        if pruned:
            save_access(state)
        return "drop"

    # Create new pairing
    code = secrets.token_hex(3)  # 6-char hex code
    now = time.time()
    state.pending[code] = {
        "sender_name": sender_name,
        "sender_id": sender_name,
        "chat_id": chat_id,
        "code": code,
        "created_at": now,
        "expires_at": now + PAIRING_EXPIRY_SECONDS,
        "reply_count": 1,
    }
    save_access(state)
    logger.info("Created pairing code %s for sender %s", code, sender_name)
    return f"pair:{code}"


def approve_pairing(code: str) -> str | None:
    """Approve a pending pairing by code. Returns the sender name, or None if not found."""
    state = load_access()
    _prune_expired(state)

    pending = state.pending.get(code)
    if not pending:
        return None

    sender_name = pending["sender_name"]
    if sender_name not in state.allow_from:
        state.allow_from.append(sender_name)
    del state.pending[code]
    save_access(state)
    logger.info("Approved pairing for %s (code: %s)", sender_name, code)
    return sender_name


def list_pending() -> list[dict]:
    """List all pending pairing requests."""
    state = load_access()
    _prune_expired(state)
    save_access(state)
    return [
        {
            "code": code,
            "sender": p["sender_name"],
            "chat_id": p.get("chat_id", ""),
            "expires_in": int(p.get("expires_at", 0) - time.time()),
        }
        for code, p in state.pending.items()
    ]


def list_allowed() -> list[str]:
    """List all allowed senders."""
    return load_access().allow_from


def remove_allowed(sender_name: str) -> bool:
    """Remove a sender from the allowlist. Returns True if removed."""
    state = load_access()
    if sender_name in state.allow_from:
        state.allow_from.remove(sender_name)
        save_access(state)
        return True
    return False
