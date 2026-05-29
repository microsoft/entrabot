"""Abstract credential store protocol.

Every platform module (mac, linux, windows) implements this protocol
so the rest of the codebase stays OS-agnostic.
"""

from __future__ import annotations

from typing import Protocol


class CredentialStore(Protocol):
    """OS-agnostic secret storage interface."""

    def store(self, service: str, key: str, value: str) -> None:
        """Persist a credential."""
        ...

    def retrieve(self, service: str, key: str) -> str | None:
        """Return the stored value, or None if not found."""
        ...

    def delete(self, service: str, key: str) -> None:
        """Remove a credential. No-op if it doesn't exist."""
        ...
