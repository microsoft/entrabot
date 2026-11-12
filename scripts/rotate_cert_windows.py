"""Rotate this computer's Blueprint certificate on Windows.

Running this script performs the supported rotation, the same one
``scripts/deploy-windows.ps1`` runs::

    python scripts/rotate_cert_windows.py [--yes] [--verbose]

which is ``python -m entrabot.winsetup rotate-cert --project-root <this repo>``. That
rotation keeps the certificate in use until the new one is proven: the new certificate is
added to the Blueprint beside the old one, the agent signs in with it, ``.env`` is switched,
and only then is the old certificate removed from the Blueprint and deleted, with its
private key, from this computer. See ``src/entrabot/winsetup/rotation.py``.

Exit codes: 0 rotated, 1 failed (the current certificate is still in use), 3 rotated but
cleanup is pending (run it again), 130 cancelled.

The rest of this module is the earlier transactional ``rotate()`` and its exceptions, kept
as a library for existing callers and their tests. It is NOT what the command above runs:
it swaps the old certificate out in the same write that adds the new one, so the agent has
no working certificate while Microsoft Entra takes the new one up. Rollback contract of
that function, when its smoke test fails:

1. Re-PATCH the original DER back to the Blueprint app.
2. Restore the previous thumbprints in ``.env``.
3. Invalidate the MSAL cache.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# .env is UTF-8 (setup writes it that way). Read with the locale codec, a non-ASCII value
# elsewhere in the file fails to decode on a cp1252 machine; "utf-8-sig" also drops a
# byte-order mark an editor may have added, which would otherwise hide the first key.
_ENV_READ_ENCODING = "utf-8-sig"
_ENV_WRITE_ENCODING = "utf-8"


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
    lines = env_path.read_text(encoding=_ENV_READ_ENCODING).splitlines()
    out = []
    for line in lines:
        if line.startswith("ENTRABOT_BLUEPRINT_CERT_THUMBPRINT="):
            out.append(f"ENTRABOT_BLUEPRINT_CERT_THUMBPRINT={x5t}")
        elif line.startswith("ENTRABOT_BLUEPRINT_CERT_SHA1="):
            out.append(f"ENTRABOT_BLUEPRINT_CERT_SHA1={sha1}")
        else:
            out.append(line)
    with env_path.open("w", encoding=_ENV_WRITE_ENCODING, newline="\n") as handle:
        handle.write("\n".join(out) + "\n")


def _read_orig_thumbprints(env_path: Path) -> tuple[str, str]:
    x5t = sha1 = ""
    for line in env_path.read_text(encoding=_ENV_READ_ENCODING).splitlines():
        if line.startswith("ENTRABOT_BLUEPRINT_CERT_THUMBPRINT="):
            x5t = line.split("=", 1)[1]
        elif line.startswith("ENTRABOT_BLUEPRINT_CERT_SHA1="):
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
        graph_patch: callable that swaps a given DER into the Blueprint
            app's ``keyCredentials``, keeping other machines' certificates
            (see ``blueprint_certs.swap_key_credential``). Must accept
            ``token`` and ``der_bytes`` kwargs and return an HTTP status
            code, >= 400 on any failure, without raising.
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


def main(argv: Sequence[str] | None = None) -> int:
    """Rotate this checkout's certificate: ``python -m entrabot.winsetup rotate-cert``."""
    parser = argparse.ArgumentParser(
        description="Rotate this computer's Blueprint certificate. The current certificate "
        "stays in use until the new one is proven to work."
    )
    parser.add_argument("--yes", action="store_true", help="Don't ask before rotating.")
    parser.add_argument(
        "--verbose", action="store_true", help="Show everything the helpers print."
    )
    args = parser.parse_args(argv)

    from entrabot.winsetup import cli

    forwarded = ["rotate-cert", "--project-root", str(Path(__file__).resolve().parent.parent)]
    if args.yes:
        forwarded.append("--yes")
    if args.verbose:
        forwarded.append("--verbose")
    return cli.main(forwarded)


if __name__ == "__main__":
    sys.exit(main())
