# `scripts/rotate_cert_windows.py`

Platforms: Windows.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

`rotate_cert_windows.py` rotates this PC's Blueprint certificate. Running it
does the same rotation as
[`deploy-windows.ps1`](../setup/deploy-windows-ps1.md): it is
`python -m entrabot.winsetup rotate-cert` for this repository. Most operators
use `deploy-windows.ps1`.

The module also still contains the earlier `rotate()` function and its
exceptions, kept as a library for existing callers and tests. That function is
**not** what the command runs. It swaps the old certificate out in the same
write that adds the new one, which leaves the agent without a working
certificate while Microsoft Entra takes up the new one.

## Requirements

- Native Windows, and the project's Python environment (`.venv`).
- A completed [`setup-windows.ps1`](../setup/setup-windows-ps1.md) run on this
  PC.
- A signed-in Azure CLI session (setup's is reused).

## Usage

```powershell
.\.venv\Scripts\python.exe scripts\rotate_cert_windows.py
.\.venv\Scripts\python.exe scripts\rotate_cert_windows.py --yes --verbose
```

## Options

- `--yes` — do not ask before rotating.
- `--verbose` — show everything the helper scripts print.

## Effects

The same five steps as `deploy-windows.ps1`, with the current certificate kept
in use until the new one is proven:

1. Create a new certificate on this PC (TPM where the TPM accepts a key).
2. Add it to the Blueprint beside the current one.
3. Sign the agent in with it, retrying while Microsoft Entra takes it up.
4. Switch `.env` to it.
5. Remove the old certificate from the Blueprint, and delete it from this PC
   with its private key.

A failure in steps 1-3 undoes the new certificate. A failure in step 5 leaves
the agent on the new certificate, and running the command again finishes the
cleanup. See the
[`deploy-windows.ps1` reference](../setup/deploy-windows-ps1.md#effects).

## Exit behavior

- `0` — rotated.
- `1` — failed. The current certificate is still in use.
- `3` — rotated, but cleanup is pending. Run it again.
- `130` — cancelled.

## Security

Only public certificates go to Graph. Graph calls use the Provisioner token,
never an `az` CLI token. Private keys are created non-exportable and are never
read; the old key is deleted with its certificate. Full thumbprints are not
printed.

## Related commands

- [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md) — the usual way to
  rotate.
- [`register_windows_blueprint_cert.py`](register-windows-blueprint-cert-py.md)
  and [`blueprint_certs.py`](blueprint-certs-py.md) — add and remove
  certificates on the Blueprint.
- [`generate_windows_cert.py`](generate-windows-cert-py.md) — creates a
  certificate the same way.
- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — repairs a PC whose
  certificate is missing.
