# generate_windows_cert.py

Generate the Blueprint certificate on Windows with hard-locked crypto
parameters, TPM-first with a software-KSP fallback, returning the thumbprints
and public certificate needed to register it on the Blueprint app. Windows only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Wraps `New-SelfSignedCertificate` with fixed cryptographic parameters so the
Blueprint certificate is always created the same way. The private key is a
non-exportable CNG key: on the TPM path it is bound to the
**Microsoft Platform Crypto Provider**; when no TPM is ready it falls back to the
**Microsoft Software Key Storage Provider**, whose key DPAPI binds to the user
profile. The certificate is created in `Cert:\CurrentUser\My`. See
[Windows Platform APIs → Blueprint certificate and non-exportable CNG key](../../../platform-docs/platform-windows.md#blueprint-certificate-and-non-exportable-cng-key)
and [Two thumbprints, two purposes](../../../platform-docs/platform-windows.md#two-thumbprints-two-purposes).

The locked parameters are RSA 2048, SHA-256, `KeyUsage DigitalSignature`, and
`KeyUsageProperty Sign`. On the TPM path `-KeyExportPolicy NonExportable` is set
explicitly.

## Requirements

- Windows with PowerShell 7 (`pwsh`) on `PATH` and `New-SelfSignedCertificate`
  available.
- A TPM is optional; without a ready TPM the command uses the software KSP.

## Usage

```powershell
# Auto-probe the TPM, generate, and export the public DER
python scripts/generate_windows_cert.py --export-der C:\path\to\blueprint.der

# Force the software KSP
python scripts/generate_windows_cert.py --ksp software
```

## Options

- `--subject` — certificate subject. Default `CN=entrabot-blueprint`.
- `--days` — validity in days. Default `365`.
- `--ksp {tpm,software}` — key storage provider. If omitted, the command probes
  `(Get-Tpm).TpmReady` and chooses `tpm` when ready, otherwise `software`,
  printing `TPM probe: chose KSP=<value>` to stderr.
- `--export-der PATH` — export the public certificate DER to `PATH` and print its
  JWT `x5t#S256` thumbprint.

## Effects

1. Selects the KSP (explicit `--ksp` or TPM auto-probe).
2. Runs `New-SelfSignedCertificate` with the locked crypto parameters, creating
   the certificate in `Cert:\CurrentUser\My`.
3. Validates the returned SHA-1 thumbprint against `^[A-F0-9]{40}$` and rejects
   any value containing line breaks, guarding against stdout corruption.
4. Prints to stdout:
   - `thumbprint=<40-hex SHA-1>` — the store-lookup key used to locate the
     certificate in `Cert:\CurrentUser\My`.
   - `ksp=<tpm|software>` — the provider actually used.
5. With `--export-der`, writes the public DER file and prints
   `x5t_s256=<base64url SHA-256, no padding>` — the thumbprint carried in the JWT
   assertion header.

The two thumbprints have distinct roles: the SHA-1 hex value is only for
certificate-store lookup, while the SHA-256 `x5t#S256` value is what goes in the
JWT header on every platform.

## Exit behavior

- `0` — the certificate was generated (and exported when requested); outputs are
  on stdout.
- Non-zero — the PowerShell certificate generation or export failed, or the
  returned thumbprint failed validation (line breaks or not 40 hex characters).
  The failure surfaces as a raised error.
- `2` — argument-parsing error (for example, an invalid `--ksp` value).

## Security

The private key is non-exportable (TPM-bound, or DPAPI-bound to the user profile
on the software path) and never leaves the certificate store. Only the public DER
and the public thumbprints are emitted.

## Common failures

- **`pwsh` not found** — install PowerShell 7 (see the Windows prerequisites).
- **TPM not ready** — the command silently falls back to the software KSP and
  notes the choice on stderr.

## Related commands

- [`rotate_cert_windows.py`](rotate-cert-windows-py.md) — reuses this helper for
  transactional rotation.
- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — first-time Windows
  provisioning that generates and registers the certificate.
- [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md) — the rotation deploy
  wrapper.
