# blueprint_certs.py

The shared logic for reading and updating the certificates registered on a Blueprint
application. A library, not a command.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Microsoft Graph treats `PATCH /applications/{id}` with `keyCredentials` as a replacement
of the whole collection, so a one-element list removes every other machine's
certificate. Everything in Entrabot that changes the list goes through this module,
which holds to three rules:

- **A failed read is never an empty list.** Any non-200 response or malformed body
  raises an error, and no `PATCH` can happen unless the `GET` before it returned 200.
- **Existing entries pass through exactly as Graph returned them.** Nothing is
  de-duplicated by display name, because short hostnames collide.
- **A write isn't done until it has been read back.** Graph is eventually consistent,
  so verification retries for a bounded time.

## Requirements

- Python dependencies `httpx` and `cryptography`, both installed with the project.
- A Graph token that can read and update the Blueprint application, supplied by the
  caller.

## Usage

Imported by sibling scripts rather than run directly:

```python
from blueprint_certs import fetch_key_credentials, register_key_credential
```

## Options

Not a CLI. The main functions are:

- `fetch_key_credentials(object_id, token)` — read the list, or raise
  `KeyCredentialsReadError`.
- `find_credential_for_public_key(credentials, public_key_der, *, now=None)` — the
  certificate for a private key's public half that can authenticate at `now` (the current
  time by default), or `None`. An expired or not-yet-valid certificate is not a match, and
  neither is an entry that isn't a `Verify` certificate. A missing or unreadable date
  doesn't rule a certificate out. If an expired and a valid certificate share a key, the
  valid one is returned.
- `merge_key_credentials(existing, new_entry, *, remove_all_existing=False)` — the list
  to send. A pure function; `remove_all_existing=True` returns only the new entry. Expired
  entries are kept like any other: nothing is pruned.
- `register_key_credential(...)` — fetch, merge, `PATCH`, then confirm by reading back.
- `remove_key_credential(*, object_id, token, x5t_s256=None, sha1_hex=None, ...)` — remove
  one certificate, identified by its key material, and keep every other machine's. The
  write counts only once a read shows that certificate gone and the others still there. A
  certificate that isn't registered is not an error. Windows rotation uses it, and Windows
  teardown uses it for this computer's setup certificate on the Provisioner app.
- `remove_key_credentials(*, object_id, token, thumbprints, must_keep=(), ...)` — remove
  several certificates in ONE read, write and confirmed read-back, keeping every other
  entry exactly as Graph returned it. Each thumbprint is a 40-character hex SHA-1 or a
  base64url SHA-256 (`x5t#S256`). The write counts only once a read shows all of them gone
  and every other entry still there. Certificates that aren't registered are not an error:
  the result's `not_listed` names them, and when none is registered nothing is sent.
  Windows teardown uses it to take this computer's certificate and a rotation leftover off
  the Blueprint together.
- `swap_key_credential(...)` — used only by the older `rotate_cert_windows.rotate()`
  library function. Drops only the entries whose key material matches the old or new
  certificate and adds the given one.

## Effects

`fetch_key_credentials` is read-only. `register_key_credential`, `remove_key_credential`,
`remove_key_credentials` and `swap_key_credential` update the Blueprint's `keyCredentials`
through Graph v1.0. Every request has a bounded timeout, and the module changes nothing
locally.

## Exit behavior

Not a CLI, so there are no exit codes. `register_key_credential` reports the outcome
through typed errors:

- `KeyCredentialsReadError` — the read before the write failed. Nothing was sent.
- `RegistrationRejectedError` — Graph refused the write with a 4xx. Nothing changed.
- `RegistrationUnconfirmedError` — the write may have landed: a 2xx that didn't become
  visible, or a 5xx or transport failure, neither of which proves nothing was written.
  Its `missing` attribute names what wasn't seen, by display name.

`swap_key_credential` never raises. It returns an HTTP-style status that is at least 400
on any failure, including a failed read, as
[`rotate_cert_windows.rotate`](rotate-cert-windows-py.md) expects.

## Security

Only public certificates pass through this module, apart from the PEM private key given
to `public_key_of_private_pem`, which is never logged. Error messages carry Graph's error
code and message, never the token or key material.

## Common failures

- **Two machines attaching in the same moment** — the read and the write aren't atomic,
  so one machine's new certificate can be lost. That machine's own verification doesn't
  see its certificate and reports it, and re-running setup there adds it again. The
  per-user lock in the older [`register_blueprint_cert.py`](register-blueprint-cert-py.md)
  only keeps two of its runs on the same machine from overlapping; this module takes no
  lock, and nothing can lock across machines.
- **Graph rejects an existing entry** — sending back an entry as Graph returned it fails
  closed: Graph's error is reported and nothing changes.
- **Removing several certificates one call at a time** — each `remove_key_credential` call
  is a whole-list write. The second call's read can land on a replica that still lists the
  certificate the first call removed, and its write then puts that certificate back, while
  both calls report success. Use `remove_key_credentials` to remove them in one write.

## Related commands

- [`register_windows_blueprint_cert.py`](register-windows-blueprint-cert-py.md) —
  registers this machine's certificate for the setup wizard and rotation, on every
  system.
- [`register_blueprint_cert.py`](register-blueprint-cert-py.md) — the older macOS and
  Linux registration, which setup no longer runs.
- [`deploy-windows.ps1`](../setup/deploy-windows-ps1.md) and
  `./scripts/setup.sh --rotate-cert` — rotation, which uses `register_key_credential`
  and then `remove_key_credential`.
- [`teardown-windows.ps1`](../teardown/teardown-windows-ps1.md) and
  [`teardown.sh`](../teardown/teardown-sh.md) — teardown, which uses
  `remove_key_credentials` for the Blueprint.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md),
  [`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md), and
  [`list_blueprint_certs.py`](list-blueprint-certs-py.md) — the read-only checks built on
  this module.
