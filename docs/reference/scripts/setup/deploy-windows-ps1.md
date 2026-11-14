# `scripts/deploy-windows.ps1`

Platforms: Windows.

## Purpose

`deploy-windows.ps1` rotates this PC's **Blueprint certificate** — it is not an
application deployment script. It replaces the certificate this PC uses to sign
the agent in, and keeps the current one in use until the new one is proven to
work. A `-Status` flag skips rotation and delegates to
[`status-windows.ps1`](../operations/status-windows-ps1.md).

It is a thin wrapper; the work is done by
`python -m entrabot.winsetup rotate-cert`.

## Requirements

- Native Windows, and Windows PowerShell 5.1 (ships with Windows) or
  PowerShell 7.
- A completed [`setup-windows.ps1`](setup-windows-ps1.md) run on this PC:
  `.venv` exists, and `.env` names the current certificate.
- A signed-in Azure CLI session (setup's is reused) with the rights setup
  needed.
- Network access to Microsoft Graph and Microsoft Entra.

## Usage

```powershell
# Rotate (asks first)
powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1

# Rotate without asking, showing everything
powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1 -Yes -Verbose

# Status only — no rotation
powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1 -Status -HealthOnly -Strict
```

## Options

- `-Yes` — do not ask before rotating. Run without a terminal (unattended),
  rotation refuses to start without it and exits `2`. Finishing an earlier
  rotation's cleanup creates nothing new, so it does not ask and needs no
  `-Yes`.
- `-Verbose` — show everything the helper scripts print.
- `-Status`, with `-Json`, `-HealthOnly`, `-Strict` — skip rotation and run
  `status-windows.ps1` with those flags.
- `-Help` — show help and exit `0`.

## Effects

1. Creates a new certificate on this PC, with a non-exportable key held by the
   TPM where the TPM accepts one.
2. Adds its public half to the agent's Blueprint, beside the current
   certificate. Every other machine's certificate is kept.
3. Signs the agent in with the new certificate, retrying while Microsoft Entra
   takes it up.
4. Switches `.env` to the new certificate.
5. Removes the old certificate from the Blueprint, and deletes it from this PC
   together with its private key.

If steps 1-3 fail, the new certificate is undone (removed from the Blueprint if
it was added, and deleted locally with its key) and nothing else changes. If
step 5 fails, the agent is already working on the new certificate; running the
script again finishes the cleanup.

Step 5 writes the Blueprint's certificate list back without the old
certificate, so it first waits for a read of that list to show the new one
(Microsoft Entra can take a minute to list a certificate that was just added).
If the new one is still not listed after about three minutes of reads, nothing
is written: the old certificate stays on the Blueprint, the script exits `3`,
and running it again later finishes the job. The old certificate's private key
is deleted from this PC either way, so the entry left on the Blueprint cannot
be used. The one leftover a second run cannot fix is a private key that could
not be deleted; the message says how to remove it by hand.

An expired certificate can still be rotated. If there is no certificate
configured, or the configured one is no longer in the certificate store, the
script changes nothing and points to `scripts\setup-windows.cmd`, which repairs
that case.

It writes a log to `%LOCALAPPDATA%\entrabot\logs\rotate-cert-<date>-<time>.log`.
Restart your assistant afterwards: the agent reads its certificate when it
starts.

## Exit behavior

- `0` — rotated; or an earlier rotation's cleanup was finished; or `-Help`.
- `1` — failed. The current certificate is still in use.
- `2` — run unattended without `-Yes`. Nothing was changed.
- `3` — rotated, but cleanup is pending. Run it again.
- `130` — cancelled.
- `1` — `.venv` is missing; run `scripts\setup-windows.cmd` first.
- `-Status` — the exit code of `status-windows.ps1`.

## Common failures

- **"Run scripts\setup-windows.cmd"** — the PC is not set up, or its
  certificate is missing from the certificate store. Setup creates and
  registers a new one.
- **Not signed in** — run `scripts\setup-windows.cmd`, which signs in, then
  rotate again.
- **The new certificate was not accepted in time** — exit `1`, with the new
  certificate undone. Run it again later.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup-windows.ps1`](setup-windows-ps1.md) — first-time setup and repair.
- [`status-windows.ps1`](../operations/status-windows-ps1.md) — status and
  health, including days to certificate expiry.
- [`teardown-windows.ps1`](../teardown/teardown-windows-ps1.md) — removes this
  PC's certificate from the Blueprint.
- [`rotate_cert_windows.py`](../auth-and-certs/rotate-cert-windows-py.md) — the
  same rotation, run from Python.
- [`setup.sh --rotate-cert`](setup-sh.md#certificate-rotation) — the same
  rotation on macOS and Linux.
