"""Linux credential store backed by Secret Service via the ``keyring`` library."""

from __future__ import annotations

import contextlib

import keyring
import keyring.errors

from entrabot.platform.keyring_backend import assert_allowed_keyring_backend


class LinuxCredentialStore:
    """Uses ``keyring`` which maps to Secret Service / KWallet on Linux."""

    def __init__(self) -> None:
        assert_allowed_keyring_backend("Linux")

    def store(self, service: str, key: str, value: str) -> None:
        keyring.set_password(service, key, value)

    def retrieve(self, service: str, key: str) -> str | None:
        return keyring.get_password(service, key)

    def delete(self, service: str, key: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(service, key)
