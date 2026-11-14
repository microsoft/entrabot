# register_blueprint_cert.py

Generate this machine's Blueprint certificate and register it on the Blueprint
application, keeping the private key in the account's single key store item
(service `entrabot`, account `blueprint-private-key`). By default this adds to the
certificates already there and removes none of them. macOS and Linux only.

Setup no longer runs this command. Before the setup wizard, `setup.sh` called it when
[`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md) found no registered
certificate for the account's key. The wizard keeps each certificate together with its
key as its own item and registers it with
[`register_windows_blueprint_cert.py`](register-windows-blueprint-cert-py.md), on every
system. On a computer set up by the wizard, do not use this command: the agent does not
read the single item while `.env` names a certificate (`ENTRABOT_BLUEPRINT_CERT_SHA1`).
Use `./scripts/setup.sh`, or `./scripts/setup.sh --rotate-cert` to replace the
certificate.

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

On a computer set up before the wizard, this is the one place that registers a
certificate for the account's single key and reconciles a key left under the pending
account by a run that did not finish.

Several machines can share one Blueprint, each with its own certificate, and Microsoft
Graph treats a `keyCredentials` update as a replacement of the whole list. This command
reads the list first and sends every existing entry back along with the new one, so
attaching a machine leaves the others working. It also keeps the new private key out of
the account the agent signs with until its certificate is confirmed on the Blueprint.

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

Earlier versions of `setup.sh` ran this for you; the current one does not.

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

Before anything else, the command takes an exclusive, non-blocking lock on
`~/.entrabot/.blueprint-cert.lock` and holds it until it exits. Overlapping runs share
the one pending-key entry, so without the lock one run could overwrite or delete the key
another needs to recover. If another setup holds the lock, the command exits `1` at once,
having read and changed nothing. The lock is per OS user and shared by every checkout,
and the kernel releases it if the process dies, so there is nothing stale to clear. It
covers one machine only; see [Common failures](#common-failures) for two machines
attaching at once.

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
7. Promotes the key to `blueprint-private-key` and reads it back.
8. Writes `BLUEPRINT_CERT_THUMBPRINT` to the state file and removes any old
   `BLUEPRINT_SECRET`. The new contents go to a temporary file beside it, which is then
   swapped in with the existing file's mode kept, so a full disk or an interruption can't
   leave the state file truncated or invalid.
9. Deletes the pending entry, and prints the thumbprint.

The pending entry goes last on purpose. It is how a later run finds the key's
certificate and pairs the two again, so it stays until the thumbprint is recorded. If the
state file can't be written, the command exits `3` with the pending entry still in place,
and the next run promotes the key again and writes the state.

After step 1, the command also looks for a pending key left by an earlier run. On a
normal run, if that key's certificate is already on the Blueprint, the command does steps
7 to 9 for it and prints the thumbprint, without writing to the Blueprint. In every other
case it reuses the pending key for the new certificate instead of generating another, so
a certificate from the earlier run that has yet to appear isn't orphaned.

An expired certificate isn't "on the Blueprint" for this purpose, and neither is one that
isn't valid yet: only a certificate that can authenticate now is a match. If the pending
key's only certificate has expired, the command registers a new certificate for that same
key. The expired entry stays on the list, because entries are never pruned.

### Removing every certificate

With `--remove-all-existing-certs`, the command lists what will be removed (display
name and expiry only) and then:

- **On a terminal**, asks `[y/N]`. Anything but yes stops with nothing changed.
- **Without a terminal**, proceeds with a loud message, because the flag itself is
  explicit consent.

Every other machine stops authenticating until it runs setup again. The Blueprint, its
Agent Identities, and its Agent Users aren't touched. To delete those, see
[`deprovision_entra_agent_identity.py`](../teardown/deprovision-entra-agent-identity-py.md).

Nothing on this machine changes until the replacement is confirmed. That includes a
pending key left by an earlier run, even when its certificate is already on the
Blueprint: a remove-all run doesn't promote it first, because the replacement is about to
remove that certificate. It reuses the key for the new certificate, and promotes it only
once the replacement is confirmed. Declining at the prompt, or Graph refusing the update,
leaves the active key, the state file, and the pending key exactly as they were.

### What happens to the key on failure

| Outcome | Blueprint | `blueprint-private-key` | Pending key |
|---|---|---|---|
| A read failed, or Graph refused the update with a 4xx | Unchanged | Unchanged | Deleted if this run generated it; **kept** if an earlier run left it |
| `--remove-all-existing-certs` declined at the prompt | Unchanged | Unchanged | Unchanged (**kept** if there was one) |
| The update may have landed but can't be confirmed (a 2xx that never becomes visible, a 5xx, or a timeout) | May have changed | Unchanged | **Kept** |
| The update is confirmed but the key can't be promoted | Changed | Unchanged | **Kept** |
| The key is promoted but the state file can't be written | Changed | Promoted | **Kept** |

The pending key is kept whenever an update may have landed, and until the thumbprint is
recorded, because after a remove-all it could be the only key that still matches
anything on the Blueprint. The next run promotes it if its certificate has appeared, and
records the thumbprint. If not, the run registers a new certificate for that same key
instead of replacing it, and a later failure leaves it in place.

## Exit behavior

- `0` — registered, confirmed, key promoted, and state file updated. The thumbprint is
  on stdout, and nothing else is.
- `1` — not registered, and nothing was changed: a read or token failure, Graph refused
  the update, the operator declined at the prompt, or another setup is running.
- `2` — usage error.
- `3` — the outcome isn't confirmed. The Blueprint may have changed, or the thumbprint
  couldn't be recorded. Re-run setup.

## Security

The private key never leaves the OS keystore except as process memory, and it's never
printed. Only the public certificate goes to Graph. Graph calls use the Provisioner
token, never an `az` CLI token, and have bounded timeouts.

## Common failures

- **Exit `1`, Graph answered 4xx** — usually missing Provisioner permissions; the
  message includes Graph's error. Nothing changed, so fix the cause and re-run.
- **Exit `1`, "Another setup is already running for this user"** — a second setup holds
  the lock. Wait for it to finish, then re-run. Nothing was read or changed. If the first
  run was killed, the kernel has already released the lock, so there is nothing to delete.
- **Exit `3`, a certificate named as not seen on the Blueprint** — either replication
  is slower than usual, or something else changed the list at the same moment, such as
  another machine attaching. The lock doesn't cover that: it only stops two setups on
  one machine overlapping. Re-run setup, then check the list with
  [`list_blueprint_certs.py`](list-blueprint-certs-py.md). If an earlier machine's
  certificate really is gone, run setup on that machine to add it back.

## Related commands

- [`setup.sh`](../setup/setup-sh.md) — the setup entry point for macOS and Linux. It
  no longer calls this.
- [`register_windows_blueprint_cert.py`](register-windows-blueprint-cert-py.md) — what
  the setup wizard registers a certificate with, on every system.
- [`blueprint_certs.py`](blueprint-certs-py.md) — the read, merge, and write logic.
- [`list_blueprint_certs.py`](list-blueprint-certs-py.md) — see which certificates are
  registered.
- [`find_local_blueprint_cert.py`](find-local-blueprint-cert-py.md) — the older check
  for the same single key item.
- [`verify_blueprint_cert.py`](verify-blueprint-cert-py.md) — checks whether a given
  thumbprint is registered; the setup wizard runs it.
