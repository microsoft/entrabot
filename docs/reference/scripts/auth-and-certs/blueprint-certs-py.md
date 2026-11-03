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
- `swap_key_credential(...)` — one step of a Windows rotation. Drops only the entries
  whose key material matches the old or new certificate and adds the given one.

## Effects

`fetch_key_credentials` is read-only. `register_key_credential` and
`swap_key_credential` update the Blueprint's `keyCredentials` through Graph v1.0. Every
request has a bounded timeout, and the module changes nothing locally.

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
  per-user lock in [`register_blueprint_cert.py`](register-blueprint-cert-py.md) only
  keeps two setups on the same machine from overlapping; this module takes no lock, and
  nothing can lock across machines.
- **Graph rejects an existing entry** — sending back an entry as Graph returned it fails
  closed: Graph's error is reported and nothing changes.

## Related commands

- [`register_blueprint_cert.py`](register-blueprint-cert-py.md) — registers this
  machine's certificate on macOS and Linux.
- [`rotate_cert_windows.py`](rotate-cert-windows-py.md) — the Windows rotation that uses
  `swap_key_credential`.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md),
  [`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md), and
  [`list_blueprint_certs.py`](list-blueprint-certs-py.md) — the read-only checks built on
  this module.
