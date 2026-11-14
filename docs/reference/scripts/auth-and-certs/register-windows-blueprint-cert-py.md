# register_windows_blueprint_cert.py

Register this machine's Blueprint certificate on the Blueprint application. It adds
the certificate and removes none of the others, unless `--remove-all-existing-certs`
is given. Windows, macOS and Linux: the file keeps the name it had when only Windows
used it.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

The setup wizard ([`setup-windows.ps1`](../setup/setup-windows-ps1.md) on Windows,
[`setup.sh`](../setup/setup-sh.md) on macOS and Linux) and certificate rotation
([`deploy-windows.ps1`](../setup/deploy-windows-ps1.md), or
`./scripts/setup.sh --rotate-cert`) call this once they have a certificate in this
account's certificate store that the Blueprint doesn't list: `Cert:\CurrentUser\My` on
Windows, the Keychain or keyring item on macOS and Linux. Creating a certificate only
puts it on the machine, so until it is registered Entra has never seen its public key
and Hop 1 fails with `invalid_client`.

Several machines can share one Blueprint, each with its own certificate, and Microsoft
Graph treats a `keyCredentials` update as a replacement of the whole list. This command
reads the list first and sends every existing entry back along with the new one, so
registering this machine leaves the others working.

## Requirements

- A provisioned Blueprint, and its object ID.
- The Provisioner app. This command uses the bootstrap token helper, which creates or
  repairs the app if needed.
- Either the certificate's SHA-1 thumbprint (`--sha1`), in which case the public
  certificate is read from this account's certificate store and no file is written, or
  its public DER file (`--der`), such as the one `generate_windows_cert.py --export-der`
  writes. A PEM file is refused.
- With `--sha1` on macOS or Linux: the account's Keychain or keyring, readable in this
  session. The certificate must be the kind setup stores, one item per certificate
  (service `entrabot-blueprint-cert`, named by its SHA-1 thumbprint).

## Usage

On Windows:

```powershell
.\.venv\Scripts\python.exe scripts\register_windows_blueprint_cert.py `
    --blueprint-object-id <BLUEPRINT_OBJECT_ID> `
    --sha1 <SHA1_THUMBPRINT>

.\.venv\Scripts\python.exe scripts\register_windows_blueprint_cert.py `
    --blueprint-object-id <BLUEPRINT_OBJECT_ID> `
    --der <PATH_TO_CERTIFICATE>
```

On macOS and Linux:

```bash
.venv/bin/python scripts/register_windows_blueprint_cert.py \
    --blueprint-object-id <BLUEPRINT_OBJECT_ID> \
    --sha1 <SHA1_THUMBPRINT>
```

Most operators reach this through setup or rotation rather than running it directly.

## Options

- `--blueprint-object-id` — the object ID of the Blueprint application. Required.
- `--sha1` — the SHA-1 thumbprint of a certificate in this account's certificate store
  (`Cert:\CurrentUser\My` on Windows, the Keychain or keyring elsewhere).
- `--der` — the certificate's public DER file.
- `--remove-all-existing-certs` — the new certificate **replaces** every certificate
  on the Blueprint, other machines' included. It never asks: setup asks the person
  before it passes this (`./scripts/setup.sh --remove-all-existing-certs`). It removes
  certificates only, never the Blueprint.

Exactly one of `--sha1` and `--der` is required.

## Effects

1. Reads the public certificate (from the certificate store for `--sha1`, or from the DER
   file) and builds a `keyCredentials` entry from it, named
   `EntraBot Device Certificate — <short hostname>`. The name is for people reading the
   list in Entra; it is never used to decide which entry is which.
2. Mints a Provisioner Graph token and reads the Blueprint's `keyCredentials`. If either
   fails, nothing is sent.
3. Sends a `PATCH` with every existing entry exactly as Graph returned it, plus the new
   one. Entries are never de-duplicated by display name, because short hostnames collide.
   With `--remove-all-existing-certs`, it sends the new entry alone.
4. Reads the list back, retrying for up to about three minutes to allow for replication
   lag, until the new certificate's thumbprint and every earlier certificate are present
   (with `--remove-all-existing-certs`, until the new certificate's thumbprint is).
5. Says how many existing certificates were kept, or, with
   `--remove-all-existing-certs`, how many were removed, with each removed one's name
   and expiry.

The private key is never read: on Windows it stays in the certificate store, and on
macOS and Linux only the certificate half of the key store item is used. Everything this
command prints goes to stderr, and stdout stays empty.

## Exit behavior

- `0` — registered, and confirmed by reading the list back.
- `1` — not registered, and nothing was changed: a token failure, the list couldn't be
  read, or Graph refused the update.
- `2` — usage error, including a DER file that is missing, unreadable, or not a DER
  certificate, and a thumbprint that isn't in this account's certificate store (or a
  store that could not be read).
- `3` — the outcome isn't confirmed. The Blueprint may have changed. Re-run setup.

The setup wizard removes a certificate it just created, together with its private key,
after exit `1` or `2`, since nothing was written. After exit `3` it keeps the certificate
and records its thumbprints in `.env`, so the next run checks the Blueprint for that same
certificate instead of creating another.

## Security

Only the public certificate goes to Graph. Graph calls use the Provisioner token, never an
`az` CLI token, and have bounded timeouts. The token, key material and the thumbprint
are never printed.

## Common failures

- **Exit `1`, Graph answered 4xx** — usually missing Provisioner permissions; the message
  includes Graph's error. Nothing changed, so fix the cause and re-run setup.
- **Exit `2`, "that certificate is not in this user's certificate store"** — the
  thumbprint is not in this account's store, or (on macOS and Linux) it names a key the
  account kept before the wizard, which has no certificate item of its own. Run setup.
- **Exit `3`, a certificate named as not seen on the Blueprint** — either replication is
  slower than usual, or something else changed the list at the same moment. Re-run setup.
  If another machine's certificate really is gone, run setup on that machine to add it
  back.

## Related commands

- [`setup-windows.ps1`](../setup/setup-windows-ps1.md) and
  [`setup.sh`](../setup/setup-sh.md) — the setup wizard that calls this.
- [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md) — rotation on Windows; on
  macOS and Linux, `./scripts/setup.sh --rotate-cert`.
- [`generate_windows_cert.py`](generate-windows-cert-py.md) — creates a certificate on
  Windows.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md) — the check that runs before
  this.
- [`blueprint_certs.py`](blueprint-certs-py.md) — the read, merge, and write logic.
- [`register_blueprint_cert.py`](register-blueprint-cert-py.md) — the older macOS and
  Linux registration, for the account's single key item; setup no longer runs it.
