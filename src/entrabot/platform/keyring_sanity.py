"""Sanity check that the OS credential store can roundtrip a 2048-bit PEM.

Phase 2 hardening (PLAN-windows-port.md). On Mac/Linux the Blueprint
private key is stored as a ~1.7 KB PEM blob via ``keyring``. Some Linux
backends (older gnome-keyring builds, certain headless Secret Service
implementations) silently truncate or refuse blobs of that size. The
existing path treats that as an opaque later failure ("acquire token
failed"); this module gives operators a clean preflight check.

Mac/Linux only. Windows is past this — its Blueprint key lives in CNG,
not in ``keyring``.
"""

from __future__ import annotations

import contextlib
import secrets
from dataclasses import dataclass

from entrabot.errors import InsecureKeyringBackendError
from entrabot.platform.base import CredentialStore
from entrabot.platform.keyring_backend import assert_allowed_keyring_backend

_SANITY_SERVICE = "entrabot-sanity"
_SANITY_KEY = "roundtrip-probe"
# ~2 KB — comfortably larger than a real 2048-bit PEM (~1700 bytes).
# Padded so any size-based truncation surfaces.
_SANITY_VALUE_BYTES = 2048


@dataclass(frozen=True)
class SanityResult:
    ok: bool
    stored_bytes: int
    backend: str | None
    diagnostic: str = ""


def check(store: CredentialStore) -> SanityResult:
    """Roundtrip a 2 KB blob through ``store``; report any backend defect.

    Always cleans up the probe entry, even on failure.
    """
    try:
        backend = assert_allowed_keyring_backend()
    except InsecureKeyringBackendError as exc:
        backend = None if exc.backend.startswith("uninspectable keyring backend") else exc.backend
        return SanityResult(
            ok=False,
            stored_bytes=0,
            backend=backend,
            diagnostic=f"insecure backend selected: {exc.backend}",
        )

    key = f"{_SANITY_KEY}-{secrets.token_hex(8)}"
    payload = secrets.token_hex(_SANITY_VALUE_BYTES // 2)  # 2 hex chars per byte
    diagnostic = ""
    ok = False
    try:
        store.store(_SANITY_SERVICE, key, payload)
    except Exception as exc:
        diagnostic = f"store() raised: {exc!r}"
        return SanityResult(
            ok=False,
            stored_bytes=len(payload),
            backend=backend,
            diagnostic=diagnostic,
        )

    try:
        retrieved = store.retrieve(_SANITY_SERVICE, key)
        if retrieved is None:
            diagnostic = "retrieve() returned None — credential is missing after store()."
        elif retrieved != payload:
            if len(retrieved) < len(payload):
                diagnostic = (
                    f"backend truncated value: stored {len(payload)} bytes, "
                    f"retrieved {len(retrieved)}"
                )
            else:
                diagnostic = "value mismatch on roundtrip — backend corrupted the blob."
        else:
            ok = True
    except Exception as exc:
        diagnostic = f"retrieve() raised: {exc!r}"
    finally:
        with contextlib.suppress(Exception):
            store.delete(_SANITY_SERVICE, key)

    return SanityResult(
        ok=ok,
        stored_bytes=len(payload),
        backend=backend,
        diagnostic=diagnostic,
    )
