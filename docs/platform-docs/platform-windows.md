# Windows Platform APIs

This page is the platform reference for how Entrabot uses Windows system
services: where its keys live, how the non-exportable Blueprint key signs the
certificate assertion, the two certificate thumbprints and their distinct
purposes, how the settings file is protected, how delegated tokens are cached,
and the setup, rotation and teardown surface.

## Keys: non-exportable, in the certificate store

Entrabot keeps two certificates on a Windows PC, both in the current user's
certificate store (`Cert:\CurrentUser\My`) and both with **non-exportable** CNG
private keys:

| Certificate | Friendly name | Used for |
|---|---|---|
| Blueprint | `Entrabot Blueprint <blueprint app id>` | Signing the agent's sign-in (Hop 1) at runtime. |
| Provisioner | `Entrabot Provisioner <tenant id>` | Signing setup's own Graph token while it creates and repairs the agent. |

No private key material is written to disk, and neither key is stored in
Windows Credential Manager. Earlier versions wrote the Provisioner key as a PEM
file under `%LOCALAPPDATA%\entrabot`. Setup replaces it with a certificate-store
key the first time it runs (`scripts/entra_provisioning.py`): it uploads the new
certificate, gets a token with it to prove Microsoft Entra accepts it, then
removes the file's certificate from the Provisioner app by thumbprint, so a copy
of the file stops working, and deletes the file. Until the new key is accepted
the file is kept, because it is still the PC's working credential. If the
removal from the app fails, the file is still deleted and setup still finishes;
the warning names the certificate (the last four characters of its thumbprint,
and its expiry date) and the App registrations path to remove it by hand, and
the next run tries again (the state key `PROVISIONER_CERT_REVOKE_PENDING`).

Both certificates are created by `entrabot.platform.wincert`
(`platform/wincert.py`), which is the one owner of the key lifecycle on Windows:
create, find, test-sign, delete.

### TPM selection is by attempt

The key is created on the **Microsoft Platform Crypto Provider** (the TPM) first.
`wincert.create_signing_cert` creates the key there and signs once with it. If
either step fails, it deletes what it created, key included, and creates the key
on the **Microsoft Software Key Storage Provider** instead, recording why.

There is no separate "is a TPM present" probe. `Get-Tpm` needs an elevated
session to report anything: for a standard user it returns an object with blank
properties rather than an error, so a probe built on it always concluded "no
TPM". Creating a key needs no elevation.

Fixed parameters, whichever provider is used: RSA 2048, SHA-256, signature-only
key usage, `-Type Custom` (no TLS client or server usages), `-KeyExportPolicy
NonExportable`, one-year validity, and the provider's default 10-minute
backdating of `NotBefore`. Creation runs through the Windows PowerShell 5.1 that
ships with Windows (`New-SelfSignedCertificate`); values are passed in
environment variables, never interpolated into script text. PowerShell 7 is not
used anywhere.

### Signing at runtime

Signing happens in-process through `ncrypt.dll`
(`auth/cncrypt_signer.py::sign_pkcs1_sha256`): the certificate is located by its
SHA-1 thumbprint, the key handle is acquired, and only the resulting signature
crosses back into the process. No process is spawned and no key material is
read. When a certificate SHA-1 is configured, the Windows sign-in path does not
open Windows Credential Manager at all.

Before signing, the runtime checks the certificate locally
(`wincert.cert_health`). A certificate that is missing, expired, not yet valid,
or has no private key on this PC produces an error that names
`scripts\setup-windows.cmd`. When 30 days or fewer remain, the agent logs a
warning once per process.

Downstream of the signed assertion, the token flow is identical across
platforms — see
[Identity and Token Flow](../architecture/identity-and-token-flow.md).

## Two thumbprints, two purposes

Windows configuration carries two distinct certificate thumbprints, and they must
not be confused (`auth/certificate.py`, `auth/cncrypt_signer.py`,
`platform/wincert.py`):

- **`ENTRABOT_BLUEPRINT_CERT_THUMBPRINT`** — the base64url-encoded SHA-256
  (`x5t#S256`) of the DER certificate. This goes in the JWT assertion header on
  every platform, Windows included.
- **`ENTRABOT_BLUEPRINT_CERT_SHA1`** — the 40-character hex SHA-1 thumbprint.
  Windows-only; used to locate the certificate in `Cert:\CurrentUser\My` and to
  identify the CNG key for `ncrypt.dll` signing.

Both are written to `.env` by the Windows setup flow. `ENTRABOT_BLUEPRINT_KSP`
records where the key was created (`tpm` or `software`) for reference; the
runtime does not read it. Setup and rotation do not print full thumbprints; the
status command's JSON output does.

## Credential storage: Windows Credential Manager

Entrabot's generic `CredentialStore` on Windows is `WindowsCredentialStore`
(`platform/windows.py`), a thin wrapper over the `keyring` library that maps to
Windows Credential Manager through `keyring.backends.Windows.WinVaultKeyring`.
At construction it calls `assert_allowed_keyring_backend("Windows")`
(`platform/keyring_backend.py`) and refuses to proceed with any other backend.

On Windows nothing on the agent's sign-in path depends on it. The one remaining
use is a last-resort lookup in the audit tool, which is non-fatal when the store
is unavailable. This matters in remote sessions: Windows does not make
Credential Manager available to an SSH logon session, even with password
sign-in.

## The settings file

`.env` is written as UTF-8 without a byte-order mark, with LF line endings, and
is read tolerantly (a byte-order mark added by an editor is ignored). Every
write goes to a temporary file that is restricted to the current user **before**
it replaces `.env`, so the file never exists with wider permissions.

"Restricted" means the file's access list is replaced with exactly one entry:
the current user, with modify rights, inheritance off (`winsetup/_winacl.py`,
through `advapi32`). It is replaced rather than edited because a new file in a
user's folder can carry SYSTEM and Administrators as explicit entries, which
`icacls /inheritance:r` does not remove. After its last write, setup reads the
access list back and only then reports the file as private; otherwise it warns
and names who else has access. `.env` holds IDs, not secrets, so that warning
does not stop setup.

## Delegated-mode token cache

In delegated mode, MSAL persists its token cache through msal-extensions'
`build_encrypted_persistence`, which on Windows encrypts the cache with DPAPI
(`auth/delegated.py`). The cache file lives under the per-user cache directory
resolved by `platformdirs` (`platformdirs.user_cache_dir("entrabot")`). If
encrypted persistence cannot be built, Entrabot logs the failure and falls back
to an in-memory cache for that run rather than writing an unencrypted cache to
disk.

## Claude Code hooks

The project's Claude Code hooks (`.claude/settings.json`) run through one
launcher, `scripts/hooks/run-hook.sh`, on every platform. Each hook is pinned to
the `bash` shell, and the two gates (blocking local memory writes, and requiring
the body prompt before Teams sends) are set to block when the hook cannot run.
The launcher uses the project's `.venv` interpreter (`.venv/Scripts/python.exe`
on Windows), then falls back to a Python on `PATH`. Hooks read their input as
UTF-8 on every platform (the launcher sets `PYTHONUTF8=1` and
`PYTHONIOENCODING=utf-8`, and each hook decodes its input itself), and the two
gates block on any unexpected error, including input they cannot decode or
parse. Blocking when a hook cannot start at all needs Claude Code 2.1.295 or
later; setup warns about an older version.

On Windows, Claude Code runs `bash` hooks through Git Bash. **Git for Windows is
therefore required to use Claude Code with this repository on Windows.** Without
it the gates block every edit and every gated Teams call.
`scripts\prereqs-windows.ps1 -WithGit` installs it for the current user without
administrator rights.

## Setup, rotation, status and teardown

All of these run on the Windows PowerShell 5.1 that ships with Windows, without
administrator rights. Run them on a native Windows host, not under WSL —
`setup-windows.ps1` refuses to run under WSL and directs WSL users to `setup.sh`.

- **`scripts/setup-windows.cmd`** is the entry point. It starts
  `setup-windows.ps1`, which is only a bootstrap: it finds Python 3.12 or newer
  (or downloads a private copy, checks the package against a pinned SHA-512
  checksum and its interpreter files' signatures before first use),
  builds `.venv`, installs Entrabot, and hands over to the setup wizard,
  `python -m entrabot.winsetup`. The wizard signs in, creates or joins the agent,
  creates and registers the certificate, writes `.env`, registers the MCP server,
  and tests the result. Re-running it is the repair path. `-Diagnose` checks an
  existing setup and changes nothing.
- **`scripts/prereqs-windows.ps1`** is optional. It installs extras on request:
  Git for Windows, Claude Code, the Work IQ tools, Visual Studio Build Tools.
- **`start-entrabot.cmd`** starts the agent in Claude Code.
- **`scripts/deploy-windows.ps1`** rotates the Blueprint certificate: a new
  certificate is added beside the current one, proven by a real sign-in, and only
  then does `.env` switch and the old one get removed from the Blueprint and
  deleted locally with its key. The old one is removed from the Blueprint only
  from a read of the list that shows the new one. Unattended, rotation runs only
  with `-Yes`.
- **`status-windows.ps1`** shows status and health. It installs nothing.
- **`scripts/teardown-windows.ps1`** disconnects this PC: it removes this PC's
  certificate from the Blueprint and deletes it locally with its key, selected by
  the configured SHA-1 thumbprint rather than by subject. A certificate that
  `.env` names but this PC's certificate store does not hold may be another
  PC's, so it is left on the Blueprint unless the person says otherwise. It
  then retires this PC's Provisioner certificate the same way, unless another
  Entrabot agent on the PC still needs it or `-KeepSetupCredential` is given.
  `-DeleteAgent` and `-PurgeData` widen the scope.
- **`scripts/generate_windows_cert.py`** and
  **`scripts/register_windows_blueprint_cert.py`** are the lower-level pieces the
  wizard uses: create a certificate through `wincert`, and add its public half to
  the Blueprint's `keyCredentials` while keeping every other machine's.

For step-by-step setup, see [Windows installation](../getting-started/windows.md).

## Default paths

- **Operational data, logs, and audit** default to a per-user directory under
  `%LOCALAPPDATA%\entrabot` (for example `%LOCALAPPDATA%\entrabot\data`), each
  overridable by its corresponding `ENTRABOT_*_DIR` environment variable
  (`config.py`).
- **Setup's private tools** live in the same folder:
  `%LOCALAPPDATA%\entrabot\python` (only when the PC had no usable Python) and
  `%LOCALAPPDATA%\entrabot\tools\azure-cli` (only when the Azure CLI was
  missing). Deleting the folder removes them, and the project's `.venv` then
  needs `scripts\setup-windows.cmd` to rebuild it.
- **The MSAL delegated token cache** lives under the `platformdirs` per-user
  cache directory for `entrabot`, as described above.

Storage files are written as UTF-8 with LF line endings on every platform, so a
file written on Windows is byte-identical to the same file written elsewhere.

## What has been verified

The Windows path was exercised end to end on one configuration: Windows 11
Enterprise 25H2 (x64) in an Azure virtual machine with a virtual TPM 2.0,
non-administrator local accounts, over SSH. On that machine: key creation,
signing and deletion against the virtual TPM; the single-entry `.env`
permission, read back; certificate rotation; teardown in its default mode
(only that PC's certificate left a Blueprint that held several) and with
`-DeleteAgent`; and the full test suite (4208 passed, 226 skipped, 0 failed).
Physical TPM chips, ARM64, Windows 10, Entra-joined or Intune-managed PCs, a
desktop session, and `-PurgeData` have not been tested. The fixes made after a
security review (described on this page and in the changelog) are covered by
unit tests but have not been run on a real Windows PC. See
[Windows installation](../getting-started/windows.md#what-has-been-tested) and
[Project status](../project/status.md).

## See also

- [Windows and Platform Support](../architecture/windows-and-platforms.md) — the
  cross-platform `CredentialStore` abstraction and per-OS certificate matrix.
- [Security Boundaries](../architecture/security-boundaries.md) — why the CNG
  path is the strongest of the three certificate paths.
- [Identity and Token Flow](../architecture/identity-and-token-flow.md) — the
  three-hop flow the signed assertion feeds into.
- [macOS Platform APIs](platform-macos.md) and
  [Linux Platform APIs](platform-linux.md) — the other two platforms.
