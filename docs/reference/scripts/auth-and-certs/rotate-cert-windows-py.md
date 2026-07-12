# rotate_cert_windows.py

Transactional Windows Blueprint certificate rotation with full rollback,
extracted from `deploy-windows.ps1` so the rollback contract is testable. Windows
only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Performs an all-or-nothing rotation of the Blueprint certificate: it registers
the new certificate on the Blueprint app, smoke-tests token acquisition, and
either commits (removing the old certificate) or rolls everything back. It is a
module invoked by [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md), not a
standalone CLI entry point. See
[Windows Platform APIs → Two thumbprints, two purposes](../../../platform-docs/platform-windows.md#two-thumbprints-two-purposes)
and [Token flows](../../token-flows.md).

## Requirements

- Windows, driven by [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md).
- The wrapper must, **before** generating the new certificate, capture the old
  certificate's public DER — for a non-exportable TPM key this is the only chance
  to grab the old public material for rollback.
- The wrapper supplies: the old DER, the new certificate's SHA-1 thumbprint,
  `x5t#S256`, and public DER (from
  [`generate_windows_cert.py`](generate-windows-cert-py.md)), plus callables for
  the Graph `PATCH`, the smoke test, deletion of the old certificate, and a Graph
  token provider, together with the `.env` path, MSAL cache path, and Blueprint
  object ID.

## Usage

Invoked through the deploy wrapper rather than run directly:

```powershell
pwsh scripts/deploy-windows.ps1
```

## Options

Not a CLI. The rotation is driven by the parameters the wrapper passes to the
`rotate` function.

## Effects

The rotation runs as an ordered transaction:

1. Read the original `x5t#S256` and SHA-1 thumbprints from `.env`.
2. Acquire a Graph token.
3. `PATCH` the new certificate DER onto the Blueprint's `keyCredentials`.
4. Rewrite the `BLUEPRINT_CERT_*` lines in `.env` to the new thumbprints (the
   single source of truth for the next run).
5. Run the smoke test (acquire a fresh Agent User token end to end).
6. On the outcome:
   - **Smoke passes** → delete the old certificate from `Cert:\CurrentUser\My`
     by its SHA-1 thumbprint. The old certificate is removed **only** after the
     new one is proven to work.
   - **Smoke fails** → roll back in order: re-`PATCH` the old DER onto the
     Blueprint, restore the original thumbprints in `.env`, and delete the MSAL
     cache so the next call does not present a token signed by the now-invalid
     new key.

## Exit behavior

Outcomes surface as typed errors that the wrapper maps to process exit codes:

- **Success** — returns without error; the new certificate is live and the old
  one removed.
- `RotationFailed` — the initial `PATCH` failed; no rollback was needed and the
  old certificate is untouched.
- `RotationRolledBack` — the `PATCH` succeeded but the smoke test failed; the
  original certificate, `.env` thumbprints, and MSAL cache state were restored.
- `ManualInterventionRequired` — both the initial `PATCH` and the rollback
  `PATCH` failed; `.env` and the MSAL cache are deliberately left untouched for
  manual triage, since the old DER is the only public material matching a working
  private key.

## Security

Private keys never leave the certificate store; only public DER is exchanged.
Invalidating the MSAL cache on rollback prevents a stale-signed token from
looping on `401` after the new key is removed.

## Common failures

- **Rollback after smoke failure** — expected and safe; the previous certificate
  is restored automatically.
- **`ManualInterventionRequired`** — re-`PATCH` the old DER manually; do not
  touch `.env`/MSAL until the Blueprint is consistent again.

## Related commands

- [`generate_windows_cert.py`](generate-windows-cert-py.md) — produces the new
  certificate material this rotation consumes.
- [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md) — the wrapper that
  drives this module.
- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — first-time Windows
  provisioning.
