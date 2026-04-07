"""Platform dispatch — selects the right credential store for the current OS."""

from __future__ import annotations

import platform as _platform

from entraclaw.platform.base import CredentialStore


def get_credential_store() -> CredentialStore:
    """Return an OS-appropriate credential store implementation."""
    system = _platform.system()
    if system == "Darwin":
        from entraclaw.platform.mac import MacCredentialStore

        return MacCredentialStore()
    elif system == "Windows":
        from entraclaw.platform.windows import WindowsCredentialStore

        return WindowsCredentialStore()
    elif system == "Linux":
        from entraclaw.platform.linux import LinuxCredentialStore

        return LinuxCredentialStore()
    raise RuntimeError(f"Unsupported platform: {system}")


__all__ = ["CredentialStore", "get_credential_store"]
