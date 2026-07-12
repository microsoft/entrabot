# list_blueprint_certs.py

Print how many certificates are registered on a Blueprint application, with one
human-readable detail line per certificate, so setup can show what will be
replaced before generating a new one. macOS and Linux only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Gives [`setup.sh`](../setup/setup-sh.md) a machine-readable count of the
Blueprint app's `keyCredentials` (on stdout) plus a human-readable summary (on
stderr) before it decides whether to generate and register a replacement
certificate. Splitting the streams lets the shell branch on the count while
still surfacing the detail lines to the operator.

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
Graph fetch fails, it treats the credential list as empty and prints `0`.

## Exit behavior

- `0` — the count was printed (including `0` when the Blueprint has no
  certificates or the fetch failed). The count on stdout is the signal, not the
  exit code.
- `2` — usage error (missing or extra positional argument).

## Security

The Provisioner token and certificate material never touch disk or logs. Only
public certificate metadata (display name and expiry) is printed.

## Common failures

- **Count is `0` unexpectedly** — the Blueprint object ID is wrong, or the Graph
  fetch failed silently; verify the ID and Provisioner permissions.

## Related commands

- [`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md) — recover the
  thumbprint that matches this machine's private key.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md) — confirm a specific
  cached thumbprint is still registered.
- [`setup.sh`](../setup/setup-sh.md) — the provisioning entry point that calls
  this helper.
