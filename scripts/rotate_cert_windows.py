"""Cert rotation logic for Windows. Extracted from ``deploy-windows.ps1``
so pytest can drive the full rollback contract end-to-end (D7).

The PS1 wrapper is responsible for: probing TPM, calling
``generate_windows_cert.generate``, computing ``new_x5t_s256``,
exporting ``new_der``, and capturing ``old_der`` from the cert that's
about to be rotated. It then hands those byte arrays (and the
``RotationState`` paths) to ``rotate`` and lets this module run the
transactional rotation.

Rollback contract — three steps when smoke fails (D13):

1. Re-PATCH the original DER back to the Blueprint app.
2. Restore the previous thumbprints in ``.env``.
3. **Invalidate the MSAL cache** — otherwise the next call presents
   a token signed by the now-invalidated new key and 401s.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class RotationFailed(RuntimeError):
    """Initial PATCH failed; no rollback was needed."""


class RotationRolledBack(RuntimeError):
    """PATCH succeeded but smoke failed; rollback completed."""


class ManualInterventionRequired(RuntimeError):
    """Both initial PATCH and rollback PATCH failed."""


@dataclass(frozen=True)
class RotationState:
    env_path: Path
    msal_cache_path: Path
    blueprint_object_id: str


GraphPatchCallable = Callable[..., int]
SmokeTestCallable = Callable[[], bool]
DeleteOldCertCallable = Callable[[str], None]


def _patch_env(env_path: Path, *, x5t: str, sha1: str) -> None:
    """Replace the BLUEPRINT_CERT_* lines in ``.env`` in place."""
    lines = env_path.read_text().splitlines()
    out = []
    for line in lines:
        if line.startswith("ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT="):
            out.append(f"ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT={x5t}")
        elif line.startswith("ENTRACLAW_BLUEPRINT_CERT_SHA1="):
            out.append(f"ENTRACLAW_BLUEPRINT_CERT_SHA1={sha1}")
        else:
            out.append(line)
    env_path.write_text("\n".join(out) + "\n")


def _read_orig_thumbprints(env_path: Path) -> tuple[str, str]:
    x5t = sha1 = ""
    for line in env_path.read_text().splitlines():
        if line.startswith("ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT="):
            x5t = line.split("=", 1)[1]
        elif line.startswith("ENTRACLAW_BLUEPRINT_CERT_SHA1="):
            sha1 = line.split("=", 1)[1]
    return x5t, sha1


def rotate(
    *,
    state: RotationState,
    old_der: bytes,
    new_thumbprint: str,
    new_x5t_s256: str,
    new_der: bytes,
    graph_patch: GraphPatchCallable,
    smoke_test: SmokeTestCallable,
    delete_old_cert: DeleteOldCertCallable,
    graph_token_provider: Callable[[], str],
) -> None:
    """Perform a transactional cert rotation with rollback.

    Args:
        state: paths + Blueprint object id.
        old_der: public DER bytes of the cert about to be rotated.
            **Must be captured before** the new cert is generated; for a
            non-exportable TPM key this is the only chance to grab it.
        new_thumbprint: SHA-1 hex of the new cert.
        new_x5t_s256: JWT ``x5t#S256`` of the new cert.
        new_der: public DER bytes of the new cert.
        graph_patch: callable that PATCHes the Blueprint app's
            ``keyCredentials`` with a given DER. Must accept
            ``token`` and ``der_bytes`` kwargs and return an HTTP
            status code.
        smoke_test: callable returning True iff a fresh agent-user
            token can be acquired.
        delete_old_cert: callable that removes the old cert from
            ``Cert:\\CurrentUser\\My`` by SHA-1 thumbprint. Only run
            after smoke succeeds.
        graph_token_provider: returns a Graph access token (typically
            an Agent Identity app-only token).
    """
    orig_x5t, orig_sha1 = _read_orig_thumbprints(state.env_path)
    token = graph_token_provider()

    # Step 1 — PATCH the new DER.
    status = graph_patch(token=token, der_bytes=new_der)
    if status >= 400:
        raise RotationFailed(
            f"Initial Graph PATCH failed with status {status}; no rollback needed."
        )

    # Step 2 — update .env atomically (single source of truth for next run).
    _patch_env(state.env_path, x5t=new_x5t_s256, sha1=new_thumbprint)

    # Step 3 — smoke test the new cert.
    try:
        passed = smoke_test()
    except Exception as exc:
        passed = False
        smoke_error: BaseException | None = exc
    else:
        smoke_error = None

    if passed:
        # Cert flip is good — delete the old cert.
        delete_old_cert(orig_sha1)
        return

    # Smoke failed → roll back.
    rollback_status = graph_patch(token=token, der_bytes=old_der)
    if rollback_status >= 400:
        # Both PATCHes failed in a row. Don't try to "fix" .env or the
        # MSAL cache — manual triage is the right move.
        raise ManualInterventionRequired(
            f"MANUAL INTERVENTION: rollback PATCH failed (status {rollback_status}); "
            "agent identity may be in an inconsistent state. "
            "The old DER is the only public material that matches a working "
            "private key in Cert:\\CurrentUser\\My; re-PATCH it manually."
        )

    _patch_env(state.env_path, x5t=orig_x5t, sha1=orig_sha1)

    # Invalidate MSAL cache (D13) — without this, the next call presents
    # a token signed under the now-invalid new public key.
    if state.msal_cache_path.exists():
        state.msal_cache_path.unlink()

    raise RotationRolledBack(
        "Smoke test failed after rotation; original cert restored. "
        + (f"(smoke error: {smoke_error!r})" if smoke_error else "")
    )
