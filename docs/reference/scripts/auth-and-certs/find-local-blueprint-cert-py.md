# find_local_blueprint_cert.py

Find the registered Blueprint certificate whose public key matches the account's older
single Blueprint private key, and print its thumbprint. macOS and Linux only.

Setup no longer runs this command. Before the setup wizard, `setup.sh` kept the
Blueprint's private key on its own, in one key store item for the whole account
(service `entrabot`, account `blueprint-private-key`), and this was the certificate check
it made. The wizard keeps each certificate together with its key as its own item, and
checks a certificate with [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md)
instead; on a computer set up before it, setup finds the older key's certificate itself
(see [Computers set up before the wizard](../setup/setup-sh.md#computers-set-up-before-the-wizard)).
This command is kept for looking at such a computer by hand.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

The thumbprint cached in the state file can be stale, because the certificate was
removed from the Blueprint, or it can belong to an earlier key: after an unconfirmed
`--remove-all-existing-certs` or a failed state write in an older `setup.sh`, the older
private key may no longer pair with it. This command checks that private key against
the certificates registered on the Blueprint and prints the SHA-256 base64url thumbprint
of the one that matches.

Only a certificate that can authenticate now is a match. One that has expired, or isn't
valid yet, is ignored even though its public key is the local key's, so this command
exits `1`. The expired entry stays on the Blueprint, because nothing here removes
entries.

If a private key is waiting under the pending keystore account
(`blueprint-private-key.pending`), a run of the older `setup.sh` did not finish. This
command says so and exits `1` without asking Graph anything. The wizard does not use a
pending key.

The keys are stored in the macOS Keychain and the Linux Secret Service; see
[macOS Platform APIs](../../../platform-docs/platform-macos.md) and
[Linux Platform APIs](../../../platform-docs/platform-linux.md).

## Requirements

- The Blueprint private key must be present in the OS keystore under the
  `entrabot` service, account `blueprint-private-key`. The command also reads the
  `blueprint-private-key.pending` account, and writes to neither. It does not read the
  wizard's per-certificate items.
- The Provisioner app must already exist with its certificate in the OS keystore;
  this command uses the existing-only token helper and never bootstraps it.
- macOS or Linux. Windows uses the SHA-1 certificate-store lookup in
  [`generate_windows_cert.py`](generate-windows-cert-py.md) and
  [`rotate_cert_windows.py`](rotate-cert-windows-py.md) instead.

## Usage

```bash
.venv/bin/python scripts/find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>
```

## Options

Positional argument only:

- `<BLUEPRINT_OBJECT_ID>` — the object ID of the Blueprint application whose
  `keyCredentials` are searched.

## Effects

1. Reads the `blueprint-private-key.pending` entry from the OS keystore, before
   anything else. If one exists, prints a one-line explanation to **stderr** and exits
   `1`; the active key is not read and Graph is not called. If the entry cannot be read,
   exits `3`, because failing to look is not the same as there being no pending key.
2. Reads the local Blueprint private key PEM from the OS keystore.
3. Mints an existing Provisioner Graph token, routing the token-helper's
   diagnostics to stderr so stdout stays clean.
4. `GET`s the Blueprint application's `keyCredentials`.
5. For each registered certificate that can authenticate now, compares the private
   key's public half (`SubjectPublicKeyInfo`) against the certificate's public key. "Can
   authenticate" means a certificate entry with `Verify` usage (or none stated) whose
   `startDateTime` has passed and whose `endDateTime` hasn't. A date that is missing or
   can't be read doesn't rule a certificate out, so a formatting surprise can't trigger
   a needless re-registration.
6. On a match, prints the certificate's SHA-256 base64url (no padding)
   thumbprint to **stdout** and a human-readable "recovered" line to **stderr**.

This command is read-only; it makes no changes to Entra or local state.

## Exit behavior

- `0` — a matching certificate was found; its thumbprint is printed to stdout.
- `1` — checked: a pending private key exists, there is no usable local private key
  in the keystore, or no valid registered certificate matches the local key (an expired
  one doesn't count). Diagnostics go to stderr.
- `2` — usage error (missing or extra positional argument).
- `3` — could not check: a keystore (the pending entry included), Graph, or token
  failure, a malformed response, or a certificate that could still be valid but whose key
  material couldn't be read. An expired certificate whose key material can't be read
  doesn't count, since it couldn't be the local key's usable match anyway.

Treat anything other than `0` or `1`, including a crash, as "could not check".

## Security

The private key never leaves the keystore's process memory, and neither the key
nor the Provisioner token is written to disk or logs. Only the public thumbprint
is emitted.

## Common failures

- **A private key is waiting under the pending account** — a registration by an older
  `setup.sh` did not finish. The wizard does not use that key: running
  [`setup.sh`](../setup/setup-sh.md) now creates a new certificate when the older
  `blueprint-private-key` has no certificate on the Blueprint.
- **No local key** — this computer has no older single key. A computer set up by the
  wizard keeps its key with its certificate instead; check it with
  `./scripts/setup.sh --diagnose`.
- **No match** — the certificate for the local key was removed from the Blueprint, or it
  has expired; [`setup.sh`](../setup/setup-sh.md) then creates and registers a new one
  for the agent.
- **Exit `3`** — usually a transient Graph failure or a locked keystore; the message on
  stderr has the cause. Fix it and re-run.

## Related commands

- [`list_blueprint_certs.py`](list-blueprint-certs-py.md) — enumerate the
  certificates registered on a Blueprint.
- [`register_blueprint_cert.py`](register-blueprint-cert-py.md) — the older
  registration, for the same single key item.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md) — checks whether a given
  thumbprint is registered; the wizard runs it on every system.
- [`setup.sh`](../setup/setup-sh.md) — the setup entry point for macOS and Linux. It no
  longer calls this helper.
