# verify_blueprint_cert.py

Check whether an expected certificate thumbprint is still registered on a
Blueprint application, so setup's cached-thumbprint fast path can detect a stale
cache before it fails at Hop 1. macOS and Linux only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

[`setup.sh`](../setup/setup-sh.md) skips certificate regeneration when
`BLUEPRINT_CERT_THUMBPRINT` is present in the state file. But if another machine
rotated the Blueprint certificate since, that cached thumbprint no longer matches
any registered public key, and the local private key fails at Hop 1 with a
cryptic `invalid_client`. This command verifies the cached thumbprint is still
registered so setup can fail fast with a clear "regenerate" signal instead. See
[Token flows](../../token-flows.md) for the certificate-to-token exchange.

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

This command is read-only. On a stale cache it prints a `[cache-desync]`
diagnostic to stderr; on success it prints nothing — the exit code is the signal.

## Exit behavior

- `0` — the expected thumbprint is present on the Blueprint's `keyCredentials`
  (the cache is valid).
- `1` — the thumbprint is not present, or the Blueprint fetch failed (treated as
  stale; setup should regenerate).
- `2` — usage error (wrong number of positional arguments).

## Security

The Provisioner token and certificate material never touch disk or logs. Only
public thumbprints are compared.

## Common failures

- **Exit `1` after a teammate re-ran setup** — the certificate was rotated
  elsewhere; regenerate locally via [`setup.sh`](../setup/setup-sh.md).
- **Wrong thumbprint format** — the expected value must be the SHA-256 base64url
  form, not the Windows SHA-1 hex thumbprint.

## Related commands

- [`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md) — recover the
  thumbprint that matches this machine's private key.
- [`list_blueprint_certs.py`](list-blueprint-certs-py.md) — enumerate the
  certificates registered on a Blueprint.
- [`setup.sh`](../setup/setup-sh.md) — the provisioning entry point that calls
  this helper.
