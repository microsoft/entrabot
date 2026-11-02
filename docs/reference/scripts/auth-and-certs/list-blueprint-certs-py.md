# list_blueprint_certs.py

Print how many certificates are registered on a Blueprint application, with one
human-readable detail line per certificate, to show which machines hold one.
macOS and Linux only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Gives an operator or a script a machine-readable count of the Blueprint app's
`keyCredentials` (on stdout) plus a human-readable summary (on stderr). Splitting
the streams lets a shell capture the count while still surfacing the detail lines.

## Requirements

- The Provisioner app must already exist with its certificate in the OS keystore;
  this command uses the existing-only token helper and never bootstraps it.
- macOS or Linux.

## Usage

```bash
python scripts/list_blueprint_certs.py <BLUEPRINT_OBJECT_ID>
```

## Options

Positional argument only:

- `<BLUEPRINT_OBJECT_ID>` — the object ID of the Blueprint application to
  inspect.

## Effects

1. Mints an existing Provisioner Graph token, routing the token-helper's
   diagnostics to stderr so stdout carries only the count.
2. `GET`s the Blueprint application's `keyCredentials`.
3. Writes output on two streams:
   - **stdout** — a single integer: the number of registered certificates.
   - **stderr** — one line per certificate in the form
     `    - <displayName>  expires <YYYY-MM-DD>` (the expiry is `endDateTime`
     truncated to the date).

This command is read-only; it makes no changes to Entra or local state. If the
list can't be read, it prints nothing on stdout, because a count that isn't known must
never look like one that is.

## Exit behavior

- `0` — the list was read and the count was printed. A `0` means the Blueprint
  really has no certificates.
- `2` — usage error (missing or extra positional argument).
- `3` — the list couldn't be read: a Graph or token failure, or a malformed response.
  Nothing is printed on stdout, and the cause is on stderr.

## Security

The Provisioner token and certificate material never touch disk or logs. Only
public certificate metadata (display name and expiry) is printed.

## Common failures

- **Exit `3`** — usually the Blueprint object ID is wrong or the Provisioner lacks
  permission; verify both.

## Related commands

- [`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md) — recover the
  thumbprint that matches this machine's private key.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md) — confirm a specific
  cached thumbprint is still registered.
- [`register_blueprint_cert.py`](register-blueprint-cert-py.md) — adds this machine's
  certificate to the list.
- [`setup.sh`](../setup/setup-sh.md) — the provisioning entry point.
