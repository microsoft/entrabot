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

from entraclaw.platform.base import CredentialStore

_SANITY_SERVICE = "entraclaw-sanity"
_SANITY_KEY = "roundtrip-probe"
# ~2 KB — comfortably larger than a real 2048-bit PEM (~1700 bytes).
# Padded so any size-based truncation surfaces.
_SANITY_VALUE_BYTES = 2048


@dataclass(frozen=True)
class SanityResult:
    ok: bool
    stored_bytes: int
    diagnostic: str = ""


def check(store: CredentialStore) -> SanityResult:
    """Roundtrip a 2 KB blob through ``store``; report any backend defect.

    Always cleans up the probe entry, even on failure.
    """
    payload = secrets.token_hex(_SANITY_VALUE_BYTES // 2)  # 2 hex chars per byte
    diagnostic = ""
    ok = False
    try:
        store.store(_SANITY_SERVICE, _SANITY_KEY, payload)
    except Exception as exc:
        diagnostic = f"store() raised: {exc!r}"
        return SanityResult(ok=False, stored_bytes=len(payload), diagnostic=diagnostic)

    try:
        retrieved = store.retrieve(_SANITY_SERVICE, _SANITY_KEY)
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
            store.delete(_SANITY_SERVICE, _SANITY_KEY)

    return SanityResult(ok=ok, stored_bytes=len(payload), diagnostic=diagnostic)
