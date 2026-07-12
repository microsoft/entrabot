"""Active-sponsor-channel binding for confused-deputy authorization fix.

Records the (chat_id, message_id, graph_sent_at) for each Agent
Identity sponsor whenever a sponsor message is **successfully pushed**
to the LLM via ``notifications/claude/channel`` (or any other inbound
delivery surface that exposes the message to the model). The bindings
are consulted by mutating tools (``add_teams_member``, ``share_file``)
to verify the sponsor named by the LLM is actively engaged in the
target chat, not merely a member of it.

Design notes (from rubber-duck review):

- Keyed by ``sponsor_user_id`` (Graph user GUID), NOT by email. Email
  is unreliable for federated / B2B identities and may be missing.
- TTL is enforced on ``graph_sent_at`` (the message's authored time
  per Graph), not on ``server_observed_at``. This prevents the
  bootstrap-replay path at ``mcp_server.py:_bootstrap_chat`` from
  minting fresh authority off an old message that was just observed.
- Future and already-expired ``graph_sent_at`` values are rejected at
  ``record()`` time — they cannot represent a live sponsor action.
- TTL of 120 seconds is intentional. This is an AUTHORIZATION window,
  not a context-freshness window. Workflows that legitimately need
  multi-minute gaps between sponsor request and agent action should
  use explicit confirmation; this binding does not implement that flow.
- Not thread-safe by design — single-process MCP-server event loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SponsorChannelBinding:
    """A live authorization binding for one sponsor."""

    sponsor_user_id: str
    chat_id: str
    graph_sent_at_epoch: float
    server_observed_at_epoch: float
    message_id: str


class ActiveChannelBindings:
    """In-memory store of active-sponsor-channel bindings.

    Not thread-safe — designed for single-process use inside the MCP
    server's event loop. If we ever split poll and tool execution
    across processes, this needs replacement with a shared store.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 120,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._bindings: dict[str, SponsorChannelBinding] = {}

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def record(
        self,
        *,
        sponsor_user_id: str,
        chat_id: str,
        graph_sent_at_epoch: float,
        message_id: str,
    ) -> bool:
        """Record a sponsor's live inbound message.

        Returns ``True`` if the binding was stored, ``False`` if
        rejected for any of:
        - empty sponsor_user_id or chat_id
        - graph_sent_at_epoch is in the future
        - graph_sent_at_epoch is already past TTL (bootstrap-replay defense)
        - an existing binding has a newer-or-equal graph_sent_at
          (ties are rejected so a replayed message with the same
          authored time cannot displace the live binding)

        ``sponsor_user_id`` is normalized to lowercase for storage.
        """
        if not sponsor_user_id or not chat_id:
            return False

        now = self._clock()

        # Reject future timestamps (clock skew or forged data). Allow
        # 1 second of slack for ordinary clock drift.
        if graph_sent_at_epoch > now + 1.0:
            return False

        # Reject already-expired timestamps — bootstrap-replay defense.
        if (now - graph_sent_at_epoch) > self._ttl_seconds:
            return False

        key = sponsor_user_id.lower()
        existing = self._bindings.get(key)
        if existing is not None and existing.graph_sent_at_epoch >= graph_sent_at_epoch:
            return False

        self._bindings[key] = SponsorChannelBinding(
            sponsor_user_id=key,
            chat_id=chat_id,
            graph_sent_at_epoch=graph_sent_at_epoch,
            server_observed_at_epoch=now,
            message_id=message_id,
        )
        return True

    def lookup(self, sponsor_user_id: str) -> SponsorChannelBinding | None:
        """Return the live binding for ``sponsor_user_id`` or ``None``.

        TTL is enforced on read; expired bindings are evicted so a clock
        rewind cannot resurrect them.
        """
        if not sponsor_user_id:
            return None
        key = sponsor_user_id.lower()
        b = self._bindings.get(key)
        if b is None:
            return None
        if (self._clock() - b.graph_sent_at_epoch) > self._ttl_seconds:
            self._bindings.pop(key, None)
            return None
        return b

    def reset(self) -> None:
        """Clear all bindings — for tests and forced re-auth."""
        self._bindings.clear()

    def snapshot(self) -> dict[str, SponsorChannelBinding]:
        """Return a shallow copy of the binding table for audit/debug."""
        return dict(self._bindings)


# Module singleton — the MCP server boot wires this in. Tests reset()
# in a fixture or instantiate their own ActiveChannelBindings().
_singleton: ActiveChannelBindings = ActiveChannelBindings()


def get_bindings() -> ActiveChannelBindings:
    """Return the process-wide singleton binding store."""
    return _singleton


def reset_for_tests() -> None:
    """Test hook — restore singleton to a clean state with default config."""
    global _singleton
    _singleton = ActiveChannelBindings()


__all__ = [
    "ActiveChannelBindings",
    "SponsorChannelBinding",
    "get_bindings",
    "reset_for_tests",
]
