# register_windows_blueprint_cert.py

Register this machine's Windows Blueprint certificate on the Blueprint application. It
adds the certificate and removes none of the others. Windows only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

[`setup-windows.ps1`](../setup/setup-windows-ps1.md) Step 6 calls this once it has a
certificate in `Cert:\CurrentUser\My` that the Blueprint doesn't list.
[`generate_windows_cert.py`](generate-windows-cert-py.md) only creates the certificate on
the machine, so until it is registered Entra has never seen its public key and Hop 1
fails with `invalid_client`.

Several machines can share one Blueprint, each with its own certificate, and Microsoft
Graph treats a `keyCredentials` update as a replacement of the whole list. This command
reads the list first and sends every existing entry back along with the new one, so
registering this machine leaves the others working.

## Requirements

- A provisioned Blueprint, and its object ID.
- The Provisioner app. This command uses the bootstrap token helper, which creates or
  repairs the app if needed.
- The certificate's public DER file, such as the one
  `generate_windows_cert.py --export-der` writes. A PEM file is refused.
- Windows. macOS and Linux use
  [`register_blueprint_cert.py`](register-blueprint-cert-py.md), which also generates
  the key.

## Usage

```powershell
python scripts/register_windows_blueprint_cert.py `
    --blueprint-object-id <BLUEPRINT_OBJECT_ID> `
    --der <PATH_TO_CERTIFICATE>
```

Most operators reach this through `setup-windows.ps1` rather than running it directly.

## Options

- `--blueprint-object-id` — the object ID of the Blueprint application. Required.
- `--der` — the certificate's public DER file. Required.

## Effects

1. Reads the DER file and builds a `keyCredentials` entry from it, named
   `EntraBot Device Certificate — <short hostname>`. The name is for people reading the
   list in Entra; it is never used to decide which entry is which.
2. Mints a Provisioner Graph token and reads the Blueprint's `keyCredentials`. If either
   fails, nothing is sent.
3. Sends a `PATCH` with every existing entry exactly as Graph returned it, plus the new
   one. Entries are never de-duplicated by display name, because short hostnames collide.
4. Reads the list back, retrying for up to about three minutes to allow for replication
   lag, until the new certificate's thumbprint and every earlier certificate are present.

The private key is never read. It stays in the certificate store. Everything this command
prints goes to stderr, and stdout stays empty.

## Exit behavior

- `0` — registered, and confirmed by reading the list back.
- `1` — not registered, and nothing was changed: a token failure, the list couldn't be
  read, or Graph refused the update.
- `2` — usage error, including a DER file that is missing, unreadable, or not a DER
  certificate.
- `3` — the outcome isn't confirmed. The Blueprint may have changed. Re-run setup.

`setup-windows.ps1` removes a certificate it just generated after exit `1` or `2`, since
nothing was written. After exit `3` it keeps the certificate and records its thumbprints
in `.env`, so the next run checks the Blueprint for that same certificate instead of
generating another.

## Security

Only the public certificate goes to Graph. Graph calls use the Provisioner token, never an
`az` CLI token, and have bounded timeouts. The token and key material are never printed.

## Common failures

- **Exit `1`, Graph answered 4xx** — usually missing Provisioner permissions; the message
  includes Graph's error. Nothing changed, so fix the cause and re-run setup.
- **Exit `3`, a certificate named as not seen on the Blueprint** — either replication is
  slower than usual, or something else changed the list at the same moment. Re-run setup.
  If another machine's certificate really is gone, run setup on that machine to add it
  back.

## Related commands

- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) — the provisioning entry point that
  calls this.
- [`generate_windows_cert.py`](generate-windows-cert-py.md) — creates the certificate this
  registers.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md) — the check that runs before
  this.
- [`blueprint_certs.py`](blueprint-certs-py.md) — the read, merge, and write logic.
- [`register_blueprint_cert.py`](register-blueprint-cert-py.md) — the macOS and Linux
  equivalent.
