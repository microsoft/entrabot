"""Linux credential store backed by Secret Service via the ``keyring`` library."""

from __future__ import annotations

import contextlib

import keyring
import keyring.errors


class LinuxCredentialStore:
    """Uses ``keyring`` which maps to Secret Service / KWallet on Linux."""

    def store(self, service: str, key: str, value: str) -> None:
        keyring.set_password(service, key, value)

    def retrieve(self, service: str, key: str) -> str | None:
        return keyring.get_password(service, key)

    def delete(self, service: str, key: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(service, key)
