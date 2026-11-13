# `scripts/generate_windows_cert.py`

Platforms: Windows.

## Purpose

`generate_windows_cert.py` creates a signing certificate with a non-exportable
key in the current user's Windows certificate store. It is a thin command line
over `entrabot.platform.wincert`, which owns the Windows key lifecycle.

The setup wizard creates the Blueprint certificate through `wincert` itself, so
you do not normally run this script. It exists for diagnostics and for other
tools that need a certificate made the same way.

## Requirements

- Native Windows. It uses the Windows PowerShell 5.1 that ships with Windows;
  PowerShell 7 is not needed.
- The project's Python environment (`.venv`).
- No administrator rights.

## Usage

```powershell
.\.venv\Scripts\python.exe scripts\generate_windows_cert.py
.\.venv\Scripts\python.exe scripts\generate_windows_cert.py --ksp software --days 90
.\.venv\Scripts\python.exe scripts\generate_windows_cert.py --export-der blueprint.cer
```

## Options

- `--subject <CN=...>` — certificate subject. Default `CN=entrabot-blueprint`.
- `--friendly-name <NAME>` — friendly name. Default `Entrabot Blueprint`.
- `--days <N>` — validity in days. Default `365`.
- `--ksp {tpm,software}` — use exactly this key store and do not fall back.
  `--ksp tpm` fails if the TPM cannot be used.
- `--export-der <PATH>` — also write the public certificate (DER) to this file.

## Effects

- Creates an RSA 2048 certificate in `Cert:\CurrentUser\My` with a
  non-exportable, signature-only key and no TLS usages.
- Without `--ksp`, tries the TPM first (Microsoft Platform Crypto Provider) by
  creating the key and signing once with it. If that fails it deletes what it
  created and uses the Microsoft Software Key Storage Provider, saying why on
  stderr. It does not probe for a TPM with `Get-Tpm`.
- Reports success only after the new key has signed once.
- Prints three machine-readable lines on stdout; everything meant for a person
  goes to stderr:

  ```text
  thumbprint=<40 uppercase hex characters>
  ksp=<tpm|software>
  x5t_s256=<43-character base64url SHA-256 of the DER>
  ```

- It does not register the certificate anywhere. Use
  [`register_windows_blueprint_cert.py`](register-windows-blueprint-cert-py.md)
  for that.

## Exit behavior

- `0` — created.
- `1` — failed. Nothing usable is left behind.
- `2` — usage error.

## Related commands

- [Script reference — Auth and certs](../index.md#auth-and-certs)
- [`register_windows_blueprint_cert.py`](register-windows-blueprint-cert-py.md) —
  adds the certificate to the Blueprint.
- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — creates and registers
  the certificate as part of setup.
- [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md) — rotation.
- [Windows Platform APIs](../../../platform-docs/platform-windows.md).
