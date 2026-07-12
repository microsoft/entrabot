# `scripts/deploy-windows.ps1`

Platforms: Windows.

## Purpose

`deploy-windows.ps1` is the Windows **Blueprint certificate-rotation**
wrapper — it is not an application deployment script. It captures the
current Blueprint certificate's public DER, generates a new certificate,
hands both to the transactional rotation logic in
[`rotate_cert_windows.py`](../auth-and-certs/rotate-cert-windows-py.md), and
only deletes the old certificate from the Windows certificate store after a
smoke test proves the new one works end to end. A `-Status` flag skips
rotation entirely and delegates to
[`status-windows.ps1`](../operations/status-windows-ps1.md).

## Requirements

- **PowerShell 7+ (`pwsh`), not Windows PowerShell 5.1** — the script checks
  `$PSVersionTable.PSVersion.Major` itself (rather than relying on a
  `#Requires` directive) so it can print an actionable "blue icon vs black
  icon" message before any PS-7-only syntax runs.
- Native Windows (`$IsWindows`); the script throws otherwise.
- A completed [`setup-windows.ps1`](setup-windows-ps1.md) run: the venv at
  `.venv\Scripts\python.exe` must exist, and `.env` must contain
  `ENTRABOT_BLUEPRINT_CERT_SHA1` identifying the certificate currently in
  `Cert:\CurrentUser\My`.
- Network access to Microsoft Graph for the `keyCredentials` `PATCH` and for
  the post-rotation smoke test's three-hop token acquisition.

## Usage

```powershell
# Rotate the Blueprint certificate
.\scripts\deploy-windows.ps1

# Status only — no rotation
.\scripts\deploy-windows.ps1 -Status
.\scripts\deploy-windows.ps1 -Status -Json
.\scripts\deploy-windows.ps1 -Status -HealthOnly -Strict
```

## Options

- `-Status` — skip cert rotation and run the consolidated status command via
  `status-windows.ps1`.
- `-Json` — with `-Status`, output machine-readable JSON.
- `-HealthOnly` — with `-Status`, only print health checks.
- `-Strict` — with `-Status`, return non-zero when health checks fail.
- `-Help` — print `Get-Help`-based documentation and exit `0`.

## Effects

1. Reads `ENTRABOT_BLUEPRINT_CERT_SHA1` from `.env` to identify the current
   certificate.
2. **Captures the current certificate's public DER before generating the new
   one** — for a non-exportable TPM-bound key this is the only chance to
   retrieve public material usable for a rollback PATCH. Writes it to a
   temporary `.cer` file under `%TEMP%`.
3. Calls [`generate_windows_cert.py`](../auth-and-certs/generate-windows-cert-py.md)
   to generate the new certificate (TPM-first, software-KSP fallback),
   exporting its public DER to a second temporary `.cer` file and capturing
   its SHA-1 thumbprint and `x5t#S256` JWT thumbprint from stdout.
4. Builds a Python driver script inline and runs it under the project venv,
   wiring a Graph `PATCH` callable, a smoke-test callable (a fresh
   `acquire_agent_user_token` call), and a certificate-deletion callable to
   [`rotate_cert_windows.rotate()`](../auth-and-certs/rotate-cert-windows-py.md),
   which performs the actual transactional rotation: `PATCH` the new DER,
   update `.env`, smoke-test, then either delete the old certificate (smoke
   passes) or roll back the `PATCH` + `.env` + MSAL cache (smoke fails). See
   that page for the full step-by-step rotation and rollback contract.
5. Deletes the old certificate from `Cert:\CurrentUser\My` **only after** the
   rotation module confirms the smoke test passed.
6. **Does not clean up the temporary old/new `.cer` DER files it writes to
   `%TEMP%`** — they are left on disk after the run, whether it succeeds or
   fails.
7. With `-Status`, none of the above runs; the script instead invokes
   `status-windows.ps1` with the forwarded status flags and exits with its
   exit code.

## Exit behavior

- `0` — rotation succeeded (new certificate live, old one deleted), or
  `-Help` was requested.
- `1` — launched from Windows PowerShell 5.1 (checked before `-Status` and
  before any rotation logic runs).
- `1` — not running on Windows, the venv is missing, `.env` has no
  `ENTRABOT_BLUEPRINT_CERT_SHA1`, `generate_windows_cert.py` failed, or the
  Python driver raised any of `RotationFailed`, `RotationRolledBack`, or
  `ManualInterventionRequired` (see the
  [rotation module's exit behavior](../auth-and-certs/rotate-cert-windows-py.md#exit-behavior)
  for what each means and what state each leaves `.env`/the certificate
  store in).
- `-Status` — exits with whatever code the underlying `status-windows.ps1`
  invocation returns.

## Common failures

- **`This script needs PowerShell 7+`** — launched from the blue-icon
  Windows PowerShell 5.1; open `pwsh` (black icon) and re-run.
- **`venv not found`** — run [`setup-windows.ps1`](setup-windows-ps1.md)
  first.
- **`ENTRABOT_BLUEPRINT_CERT_SHA1 missing from .env`** — the machine has not
  completed initial setup, or `.env` was hand-edited; re-run
  [`setup-windows.ps1`](setup-windows-ps1.md).
- **Rotation failed with no rollback needed** — the initial Graph `PATCH`
  itself failed; the old certificate is untouched and still active.
- **Rotation rolled back** — the new certificate's smoke test failed after
  the `PATCH` succeeded; the original certificate, `.env` thumbprints, and
  MSAL cache were restored automatically. Re-run once the underlying issue
  (for example, license replication delay) clears.
- **`ManualInterventionRequired`** — both the initial and rollback `PATCH`
  calls failed; `.env` and the MSAL cache are deliberately left as-is for
  manual triage, since the old DER may be the only public material matching
  a still-working private key.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`rotate_cert_windows.py`](../auth-and-certs/rotate-cert-windows-py.md) —
  the transactional rotation and rollback logic this wrapper drives.
- [`generate_windows_cert.py`](../auth-and-certs/generate-windows-cert-py.md) —
  generates the new certificate material.
- [`setup-windows.ps1`](setup-windows-ps1.md) — first-time Windows
  provisioning that this wrapper assumes has already run.
- [`status-windows.ps1`](../operations/status-windows-ps1.md) — status/health
  reporting reached via `-Status`.
