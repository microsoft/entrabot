# find_local_blueprint_cert.py

Recover the registered Blueprint certificate thumbprint that matches this
machine's local private key, so setup can reuse an existing certificate instead
of registering another. macOS and Linux only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Worktree-local setup state can be missing `BLUEPRINT_CERT_THUMBPRINT` even when
the machine already holds the Blueprint private key in the OS keystore and the
matching public certificate is still registered on the Blueprint app. This
command finds the registered certificate whose public key matches the local
private key and prints its SHA-256 base64url thumbprint, letting
[`setup.sh`](../setup/setup-sh.md) reuse it rather than adding another.

The private key is stored on macOS Keychain and the Linux Secret Service; see
[macOS Platform APIs](../../../platform-docs/platform-macos.md) and
[Linux Platform APIs](../../../platform-docs/platform-linux.md).

## Requirements

- The Blueprint private key must be present in the OS keystore under the
  `entrabot` service, account `blueprint-private-key`.
- The Provisioner app must already exist with its certificate in the OS keystore;
  this command uses the existing-only token helper and never bootstraps it.
- macOS or Linux. Windows uses the SHA-1 certificate-store lookup in
  [`generate_windows_cert.py`](generate-windows-cert-py.md) and
  [`rotate_cert_windows.py`](rotate-cert-windows-py.md) instead.

## Usage

```bash
python scripts/find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>
```

## Options

Positional argument only:

- `<BLUEPRINT_OBJECT_ID>` — the object ID of the Blueprint application whose
  `keyCredentials` are searched.

## Effects

1. Reads the local Blueprint private key PEM from the OS keystore.
2. Mints an existing Provisioner Graph token, routing the token-helper's
   diagnostics to stderr so stdout stays clean.
3. `GET`s the Blueprint application's `keyCredentials`.
4. For each registered certificate, compares the private key's public half
   (`SubjectPublicKeyInfo`) against the certificate's public key.
5. On a match, prints the certificate's SHA-256 base64url (no padding)
   thumbprint to **stdout** and a human-readable "recovered" line to **stderr**.

This command is read-only; it makes no changes to Entra or local state.

## Exit behavior

- `0` — a matching certificate was found; its thumbprint is printed to stdout.
- `1` — checked: no usable local private key in the keystore, or no registered
  certificate matches the local key. Diagnostics go to stderr.
- `2` — usage error (missing or extra positional argument).
- `3` — could not check: a keystore, Graph, or token failure, a malformed response, or
  a certificate whose key material couldn't be read. Setup stops and does not generate
  a certificate.

Anything other than `0` or `1`, including a crash, is treated by setup as "could not
check".

## Security

The private key never leaves the keystore's process memory, and neither the key
nor the Provisioner token is written to disk or logs. Only the public thumbprint
is emitted.

## Common failures

- **No local key** — this machine has never generated the Blueprint key; run
  [`setup.sh`](../setup/setup-sh.md).
- **No match** — the certificate for the local key was removed from the Blueprint;
  [`setup.sh`](../setup/setup-sh.md) adds a new one.
- **Exit `3`** — usually a transient Graph failure or a locked keystore; the message on
  stderr has the cause. Fix it and re-run.

## Related commands

- [`list_blueprint_certs.py`](list-blueprint-certs-py.md) — enumerate the
  certificates registered on a Blueprint.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md) — confirm a specific
  cached thumbprint is still registered.
- [`setup.sh`](../setup/setup-sh.md) — the provisioning entry point that calls
  this helper.
