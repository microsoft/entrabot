# verify_blueprint_cert.py

Check whether a given certificate thumbprint is still registered on a Blueprint
application. An operator tool: [`setup.sh`](../setup/setup-sh.md) does not call it.
macOS and Linux only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

A certificate can be removed from a Blueprint after it was registered, by an admin or
by `--remove-all-existing-certs` on another machine. The private key that was paired
with it then fails at Hop 1 with a cryptic `invalid_client`. This command answers one
question: is this thumbprint, for example the one in `.env` or in the state file, still
on the Blueprint? See [Token flows](../../token-flows.md) for the certificate-to-token
exchange.

Setup used to call this command to vouch for the thumbprint cached in the state file. It
no longer does. A thumbprint that is on the Blueprint does not show that this machine's
private key matches it, and the cache can belong to an earlier key. Setup relies on
[`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md) instead, which checks
the local key against the registered certificates.

## Requirements

- The Provisioner app must already exist with its certificate in the OS keystore;
  this command uses the existing-only token helper and never bootstraps it.
- macOS or Linux.

## Usage

```bash
python scripts/verify_blueprint_cert.py <BLUEPRINT_OBJECT_ID> <EXPECTED_THUMBPRINT>
```

## Options

Positional arguments only:

- `<BLUEPRINT_OBJECT_ID>` — the object ID of the Blueprint application to check.
- `<EXPECTED_THUMBPRINT>` — the SHA-256 base64url thumbprint expected to be
  present.

## Effects

1. Mints an existing Provisioner Graph token, routing the token-helper's
   diagnostics to stderr.
2. `GET`s the Blueprint application's `keyCredentials`.
3. Computes the SHA-256 base64url (no padding) thumbprint of each registered
   certificate's DER and compares it to the expected value.

This command is read-only. When the thumbprint is not on the Blueprint it prints a
`[cache-desync]` diagnostic to stderr; on success it prints nothing — the exit code is
the signal.

## Exit behavior

- `0` — the expected thumbprint is present on the Blueprint's `keyCredentials`.
- `1` — checked: the thumbprint is not present. Run [`setup.sh`](../setup/setup-sh.md)
  to add a certificate for this machine.
- `2` — usage error (wrong number of positional arguments).
- `3` — could not check: a Graph or token failure, a malformed response, or a
  certificate whose key material couldn't be read. This says nothing about whether the
  thumbprint is registered.

Treat anything other than `0` or `1`, including a crash, as "could not check".

## Security

The Provisioner token and certificate material never touch disk or logs. Only
public thumbprints are compared.

## Common failures

- **Exit `1`** — the certificate was removed from the Blueprint, for example by an
  admin or by `--remove-all-existing-certs` on another machine. Run
  [`setup.sh`](../setup/setup-sh.md) to add a new one.
- **Exit `3`** — usually a transient Graph failure or missing Provisioner permissions;
  the message on stderr has the cause. Fix it and re-run.
- **Wrong thumbprint format** — the expected value must be the SHA-256 base64url
  form, not the Windows SHA-1 hex thumbprint.

## Related commands

- [`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md) — find the
  registered certificate that matches this machine's private key. This is the check
  setup makes.
- [`list_blueprint_certs.py`](list-blueprint-certs-py.md) — enumerate the
  certificates registered on a Blueprint.
- [`setup.sh`](../setup/setup-sh.md) — the provisioning entry point. It does not call
  this command.
