# register_blueprint_cert.py

Generate this machine's Blueprint certificate and register it on the Blueprint
application. By default this adds to the certificates already there and removes
none of them. macOS and Linux only.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

[`setup.sh`](../setup/setup-sh.md) Step 6 calls this when the machine has no
certificate that is still registered. Several machines can share one Blueprint, each
with its own certificate, and Microsoft Graph treats a `keyCredentials` update as a
replacement of the whole list. This command reads the list first and sends every
existing entry back along with the new one, so attaching a machine leaves the others
working. It also keeps the new private key out of the account the agent signs with
until its certificate is confirmed on the Blueprint.

## Requirements

- An OS-native credential store reachable by `keyring`. The command fails closed
  before generating anything if the active backend isn't OS-native.
- The Provisioner app. This command uses the bootstrap token helper, which creates or
  repairs the app if needed.
- macOS or Linux. Windows uses
  [`generate_windows_cert.py`](generate-windows-cert-py.md) and
  [`rotate_cert_windows.py`](rotate-cert-windows-py.md) instead.

## Usage

```bash
python scripts/register_blueprint_cert.py \
    --blueprint-object-id <BLUEPRINT_OBJECT_ID> \
    --blueprint-app-id <BLUEPRINT_APP_ID> \
    --state-file <PATH_TO_STATE_FILE>
```

Most operators reach this through `setup.sh` rather than running it directly.

## Options

- `--blueprint-object-id` — the object ID of the Blueprint application. Required.
- `--blueprint-app-id` — the Blueprint's app ID, used in the certificate subject.
  Required.
- `--state-file` — the state file to record the thumbprint in. Required.
- `--remove-all-existing-certs` — remove **every** certificate on the Blueprint,
  including those of other machines, and replace them with this machine's new one.
  **This does not delete the Blueprint itself.** See
  [Removing every certificate](#removing-every-certificate).

## Effects

1. Checks the `keyring` backend, mints a Provisioner Graph token, and reads the
   Blueprint's `keyCredentials`. If any of this fails, nothing is generated.
2. Prints `adding to N existing certificate(s)`, or with
   `--remove-all-existing-certs`, asks for consent.
3. Generates an RSA-2048 key and a 365-day self-signed certificate.
4. Stores the private key under the temporary keystore account
   `blueprint-private-key.pending` and reads it back to confirm.
5. Reads `keyCredentials` again, then sends a `PATCH` with every existing entry exactly
   as Graph returned it, plus the new one. Entries are never de-duplicated by display
   name, because short hostnames collide and many Macs are simply named `MacBook-Pro`.
6. Reads the list back, retrying for up to about three minutes to allow for replication
   lag, until the new certificate's thumbprint and every earlier `keyId` are present.
7. Promotes the key to `blueprint-private-key`, reads it back, and deletes the pending
   entry.
8. Writes `BLUEPRINT_CERT_THUMBPRINT` to the state file, removes any old
   `BLUEPRINT_SECRET`, and prints the thumbprint.

### Removing every certificate

With `--remove-all-existing-certs`, the command lists what will be removed (display
name and expiry only) and then:

- **On a terminal**, asks `[y/N]`. Anything but yes stops with nothing changed.
- **Without a terminal**, proceeds with a loud message, because the flag itself is
  explicit consent.

Every other machine stops authenticating until it runs setup again. The Blueprint, its
Agent Identities, and its Agent Users aren't touched. To delete those, see
[`deprovision_entra_agent_identity.py`](../teardown/deprovision-entra-agent-identity-py.md).

### What happens to the key on failure

| Outcome | Blueprint | `blueprint-private-key` | Pending key |
|---|---|---|---|
| A read failed, or Graph refused the update with a 4xx | Unchanged | Unchanged | Deleted |
| The update may have landed but can't be confirmed (a 2xx that never becomes visible, a 5xx, or a timeout) | May have changed | Unchanged | **Kept** |
| The update is confirmed but the key can't be promoted | Changed | Unchanged | **Kept** |

The pending key is kept whenever an update may have landed, because after a remove-all
it could be the only key that still matches anything on the Blueprint. The next run
promotes it if its certificate has appeared. If not, the run registers a new certificate
for that same key instead of replacing it, and a later failure leaves it in place.

## Exit behavior

- `0` — registered, confirmed, key promoted, and state file updated. The thumbprint is
  on stdout, and nothing else is.
- `1` — not registered, and nothing was changed: a read or token failure, Graph refused
  the update, or the operator declined at the prompt.
- `2` — usage error.
- `3` — the outcome isn't confirmed. The Blueprint may have changed. Re-run setup.

## Security

The private key never leaves the OS keystore except as process memory, and it's never
printed. Only the public certificate goes to Graph. Graph calls use the Provisioner
token, never an `az` CLI token, and have bounded timeouts.

## Common failures

- **Exit `1`, Graph answered 4xx** — usually missing Provisioner permissions; the
  message includes Graph's error. Nothing changed, so fix the cause and re-run.
- **Exit `3`, a certificate named as not seen on the Blueprint** — either replication
  is slower than usual, or something else changed the list at the same moment. Re-run
  setup, then check the list with
  [`list_blueprint_certs.py`](list-blueprint-certs-py.md). If an earlier machine's
  certificate really is gone, run setup on that machine to add it back.

## Related commands

- [`setup.sh`](../setup/setup-sh.md) — the provisioning entry point that calls this.
- [`blueprint_certs.py`](blueprint-certs-py.md) — the read, merge, and write logic.
- [`list_blueprint_certs.py`](list-blueprint-certs-py.md) — see which certificates are
  registered.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md) and
  [`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md) — the checks that
  run before this.
