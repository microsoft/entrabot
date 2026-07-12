# provisioner-token.py

Print a Microsoft Graph access token minted with the Provisioner app's
certificate, for manual Graph calls during debugging. The token is written to
stdout; all diagnostics and errors go to stderr.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

Provides a clean bearer token for the Provisioner app so operators can make
ad-hoc Graph calls (for example with `curl`) without any client-secret path. The
certificate private key is read from the OS keystore in memory only; nothing is
written to disk. It replaces older curl-with-client-secret flows. See
[Token flows](../../token-flows.md) and [Auth](../../api/auth.md) for how tokens
are acquired.

## Requirements

- The Provisioner app must already be bootstrapped, with its certificate private
  key present in the OS keystore. This command uses the existing-only token
  helper and never creates or repairs the Provisioner app.
- A Python environment with the provisioning dependencies installed.

## Usage

```bash
# Print the token
python3 scripts/provisioner-token.py

# Capture into an environment variable (recommended)
TOKEN=$(python3 scripts/provisioner-token.py)
```

Capturing into a variable relies on the token being the only thing on stdout,
which this command guarantees by routing the token-helper's diagnostic output to
stderr.

## Options

None.

## Effects

1. Reads the Provisioner certificate private key from the OS keystore in memory
   only.
2. Acquires a Graph access token via the certificate credential, redirecting the
   helper's diagnostic prints to stderr so stdout carries only the token.
3. Prints the token to stdout. Nothing is written to disk.

## Exit behavior

- `0` — success; the access token is printed as a single line on stdout.
- `1` — the Provisioner app could not be used (for example, it has not been
  bootstrapped, or its certificate is unavailable). The error is printed to
  stderr and stdout stays empty.

## Security

- The printed value is a **bearer token** for Microsoft Graph carrying the
  Provisioner app's application permissions. Anyone who holds it can act as the
  Provisioner until the token expires.
- Do not paste it into shared logs, screen recordings, chat, or files, and do
  not pass it as a command-line argument (it would land in shell history).
  Prefer capturing it into an environment variable.
- Redirecting stdout to a file writes the token to disk — avoid this.
- Never redirect stderr to `/dev/null`; doing so hides the error that explains a
  failed run.

## Common failures

- **Provisioner not bootstrapped** — the token helper raises a bootstrap error;
  run [`setup.sh`](../setup/setup-sh.md) to provision it first.
- **Keystore unavailable** — the certificate private key cannot be read from the
  OS keystore (locked keychain, wrong host, or missing key).

## Related commands

- [`grant_consent.py`](grant-consent-py.md) — grant delegated scopes.
- [`revoke_consent.py`](revoke-consent-py.md) — remove delegated scopes.
- [`show_permissions.py`](../operations/show-permissions-py.md) — inspect the
  delegated grants.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) —
  provisions the identity chain the Provisioner manages.
