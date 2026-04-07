"""Windows credential store backed by Windows Credential Locker via ``keyring``."""

from __future__ import annotations

import contextlib

import keyring
import keyring.errors


class WindowsCredentialStore:
    """Uses ``keyring`` which maps to Windows Credential Locker."""

    def store(self, service: str, key: str, value: str) -> None:
        keyring.set_password(service, key, value)

    def retrieve(self, service: str, key: str) -> str | None:
        return keyring.get_password(service, key)

    def delete(self, service: str, key: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(service, key)
