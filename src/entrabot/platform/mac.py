"""macOS credential store backed by Keychain via the ``keyring`` library."""

from __future__ import annotations

import contextlib

import keyring
import keyring.errors


class MacCredentialStore:
    """Uses ``keyring`` which maps to macOS Keychain by default."""

    def store(self, service: str, key: str, value: str) -> None:
        keyring.set_password(service, key, value)

    def retrieve(self, service: str, key: str) -> str | None:
        return keyring.get_password(service, key)

    def delete(self, service: str, key: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(service, key)
