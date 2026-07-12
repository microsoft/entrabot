# Windows

## Setup says PowerShell is too old

Run Entrabot from native **PowerShell 7+** (`pwsh`), not Windows PowerShell 5.1
and not WSL:

```powershell
pwsh --version
scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id>
```

If `pwsh` is missing, run `scripts\prereqs-windows.ps1`, reopen the terminal,
and retry. WSL is a separate Linux environment; use `scripts/setup.sh` inside
WSL instead of the Windows certificate path.

## Setup says Python is unsupported

Python 3.12 or newer is required:

```powershell
python -c "import sys; print(sys.version)"
```

On Windows ARM64 with Python 3.13, a `cryptography` install may attempt a local
native build instead of selecting a binary wheel. Before the editable
install/setup, upgrade pip and require a wheel:

```powershell
python -m pip install --only-binary :all: "cryptography>=46"
```

Then re-run setup. If no compatible wheel is available for that interpreter,
create the venv with Python 3.12 rather than weakening certificate handling or
installing an untrusted binary.

## The certificate exists but JWT authentication fails

Windows uses two thumbprints for different purposes:

| Variable | Format | Purpose |
|---|---|---|
| `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` | Base64url SHA-256 | JWT assertion header `x5t#S256` |
| `ENTRABOT_BLUEPRINT_CERT_SHA1` | 40-character hexadecimal SHA-1 | Locate the certificate and CNG key in `Cert:\CurrentUser\My` |

Do not put the SHA-1 value in `x5t#S256`, and do not try to locate the Windows
certificate by the base64url SHA-256 value.

Inspect the current-user certificate store without exporting private keys:

```powershell
Get-ChildItem Cert:\CurrentUser\My |
  Where-Object { $_.Subject -eq 'CN=entrabot-blueprint' } |
  Select-Object Subject, Thumbprint, NotBefore, NotAfter, HasPrivateKey
```

Re-run setup if the local certificate and `.env` no longer agree.

## Setup reports TPM or key-provider errors

Certificate generation is TPM-first:

1. Use Microsoft Platform Crypto Provider when a ready TPM is available.
2. Fall back to Microsoft Software Key Storage Provider otherwise.

The TPM path uses a non-exportable private key. The software fallback is
supported and protected by the current user's profile/DPAPI, but the source
does not claim it is non-exportable. Confirm that the current user can create
a certificate in `Cert:\CurrentUser\My`, then re-run setup.

## Entrabot reports a legacy data-directory conflict

Current Windows state belongs under:

```text
%LOCALAPPDATA%\entrabot
```

Older installations may have data under `%USERPROFILE%\.entrabot`.

- If only the legacy directory contains data, rerun setup normally, for example:

  ```powershell
  scripts\setup-windows.cmd -UseBlueprint <blueprint-app-id>
  ```

- If both directories contain data, setup stops for manual triage. Compare
  timestamps and contents, preserve a backup, choose the authoritative root,
  and remove or relocate the other directory before retrying.

`-Migrate` is compatibility-only; the legacy migration runs unconditionally on
every setup invocation.

Do not merge cursor files blindly; replay protection depends on their current
timestamps and message-ID tails.

## Certificate rotation failed

Use the transactional rotation wrapper:

```powershell
pwsh -File scripts\deploy-windows.ps1
```

Rotation captures the old public DER before creating the new non-exportable
key, registers the new certificate, updates both thumbprints, and smoke-tests
Agent User authentication. If the smoke test fails, it restores the old
certificate registration and `.env` values and invalidates the MSAL cache.

If rotation reports `ManualInterventionRequired`, stop normal use until the
Blueprint's registered public certificate and the local private key are
consistent again.

## I need to remove this machine's local Windows state

Run:

```powershell
pwsh -File scripts\teardown-windows.ps1
```

This removes local Entrabot certificates, `%LOCALAPPDATA%\entrabot`, relevant
certificate lines from `.env`, the local MSAL cache, and MCP registrations.
It does **not** delete the Blueprint, Agent Identity, Agent User, licenses, or
other tenant resources.

See [Windows Platform APIs](../platform-docs/platform-windows.md) and
[Identity Lifecycle](../guides/identity-lifecycle.md).
