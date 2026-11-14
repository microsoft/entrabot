# Identity Lifecycle and Deprovisioning

This guide covers the resource chain Entrabot provisions in Entra, the
runtime identity states the MCP server moves through, and how to inspect,
rotate, and tear down that chain.

## Provisioning order

Provisioning creates four resources, in order:

1. **Blueprint application** — the Agent Identity Blueprint app registration.
2. **BlueprintPrincipal** — created explicitly as its own step; it is not
   auto-created alongside the Blueprint application.
3. **Agent Identity** — a service principal scoped to the Blueprint.
4. **Agent User** — a real Entra user account linked to the Agent Identity,
   which is what gives the agent Teams presence and an `idtyp=user` token.

See the
[`create_entra_agent_ids.py` reference](../reference/scripts/provisioning/create-entra-agent-ids-py.md)
for the script that runs this chain.

## Runtime identity states

The MCP server's identity state machine is not a strict linear progression
— it defines a specific set of allowed transitions:

| From | Allowed transitions to |
|------|------------------------|
| `UNAUTHENTICATED` | `DELEGATED`, `AGENT_USER` |
| `DELEGATED` | `PROVISIONING`, `UNAUTHENTICATED` |
| `PROVISIONING` | `AGENT_USER`, `ERROR`, `DELEGATED` |
| `AGENT_USER` | `ERROR`, `UNAUTHENTICATED` |
| `ERROR` | `DELEGATED`, `UNAUTHENTICATED` |

Every transition is validated against this table and applied atomically. If
the transition fails partway through, it rolls back cleanly and does not
leave partial session state behind.

To inspect the current state and health of the chain — Blueprint, Agent
Identity, Agent User, sponsors, permissions, certificates, licenses, and
storage configuration — run:

macOS/Linux:

```bash
.venv/bin/python3 scripts/show_agent_status.py
```

Windows (PowerShell):

```powershell
.\.venv\Scripts\python.exe scripts\show_agent_status.py
```

See the
[`show_agent_status.py` reference](../reference/scripts/operations/show-agent-status-py.md).

## Certificate lifecycle

Each computer signs the agent in with its own certificate, whose private key stays on
that computer: on Windows a non-exportable key in the certificate store (in the TPM
where the TPM accepts one), on macOS and Linux one Keychain or keyring item that holds
the certificate and its key together. `.env` names it by two thumbprints: the
`x5t#S256` (SHA-256) thumbprint used in JWT assertion headers, and the 40-character
SHA-1 thumbprint that finds the certificate and its key in the store. Setup reuses the
certificate on every run when it is in the store, in date, can sign, and is on the
Blueprint ([`verify_blueprint_cert.py`](../reference/scripts/auth-and-certs/verify-blueprint-cert-py.md)
checks that); otherwise it creates and registers a new one.

Rotate the Blueprint certificate with:

macOS/Linux:

```bash
./scripts/setup.sh --rotate-cert
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1
```

The certificate in use stays in use until the new one is proven to work:

1. A new certificate is created on this computer: on Windows with its key in the TPM
   where the TPM accepts one, on macOS and Linux in the account's Keychain or keyring.
2. Its public half is added to the Blueprint beside the current certificate. Other
   machines' certificates are kept.
3. The agent signs in with the new certificate, retrying while Microsoft Entra takes
   it up.
4. `.env` switches to the new certificate's two thumbprints.
5. The old certificate is removed from the Blueprint and deleted from this computer
   together with its private key.

A failure in steps 1-3 undoes the new certificate and changes nothing else. A failure
in step 5 leaves the agent working on the new certificate; running the command again
finishes the cleanup. Restart the assistant afterwards, because the agent reads its
certificate when it starts. A certificate is valid for one year, and the agent logs a
warning when 30 days or fewer remain. See the
[`deploy-windows.ps1` reference](../reference/scripts/setup/deploy-windows-ps1.md) and
[`setup.sh --rotate-cert`](../reference/scripts/setup/setup-sh.md#certificate-rotation).
On macOS and Linux, rotation has unit tests only so far.

To disconnect a computer from an agent, run `./scripts/teardown.sh` (macOS and Linux)
or `scripts\teardown-windows.ps1` (Windows). It removes that computer's certificate
from the Blueprint, keeping other machines', and deletes it locally with its key; a
certificate left by an interrupted rotation comes off in the same write. It also
retires that computer's setup (Provisioner) certificate (removed from the Provisioner
app and deleted locally with its key) unless another Entrabot agent there still needs
it or `--keep-setup-credential` (`-KeepSetupCredential`) is given. Before that, it
tries to remove any older setup certificate that setup noted it could not take off the
Provisioner app, and reports where to remove it by hand if that fails. See the
[`teardown.sh` reference](../reference/scripts/teardown/teardown-sh.md) and the
[`teardown-windows.ps1` reference](../reference/scripts/teardown/teardown-windows-ps1.md).

**A macOS or Linux computer set up before the setup wizard** kept the private key on
its own, in one key store item for the whole account (`entrabot` /
`blueprint-private-key`), with no SHA-1 thumbprint in `.env`. It keeps working; the
next `./scripts/setup.sh` stores the key with its certificate as the agent's own item
when that certificate is on the Blueprint. Run setup once before rotating or tearing
down such a computer. The older scripts for that single item,
[`find_local_blueprint_cert.py`](../reference/scripts/auth-and-certs/find-local-blueprint-cert-py.md)
and
[`register_blueprint_cert.py`](../reference/scripts/auth-and-certs/register-blueprint-cert-py.md),
are no longer run by setup.

### Several machines on one Blueprint

A Blueprint holds one certificate per machine, and attaching a machine with
`./scripts/setup.sh --use-blueprint=<APP_ID>` (`scripts\setup-windows.cmd -UseBlueprint
<APP_ID>` on Windows) adds to the list without removing any other machine's
certificate. See which certificates are registered with
[`list_blueprint_certs.py`](../reference/scripts/auth-and-certs/list-blueprint-certs-py.md).

To start over, for example after losing a laptop,
`./scripts/setup.sh --use-blueprint=<APP_ID> --remove-all-existing-certs` removes
**every** certificate on the Blueprint and replaces them with a new one for the macOS
or Linux computer you run it on. Every other machine stops authenticating until it
runs setup again. At a terminal setup asks first, with No suggested. The flag only
removes certificates: it does not delete the Blueprint, its Agent Identities, or its
Agent Users. To delete those, see [Targeted deprovisioning](#targeted-deprovisioning).
See the
[`register_windows_blueprint_cert.py` reference](../reference/scripts/auth-and-certs/register-windows-blueprint-cert-py.md).

## Targeted deprovisioning

To remove a single Agent User chain, first run a dry run — the target UPN is
required:

macOS/Linux:

```bash
.venv/bin/python3 scripts/deprovision_entra_agent_identity.py --agent-user-upn agent@tenant.onmicrosoft.com --dry-run
```

Windows (PowerShell):

```powershell
.\.venv\Scripts\python.exe scripts\deprovision_entra_agent_identity.py --agent-user-upn agent@tenant.onmicrosoft.com --dry-run
```

Review the output, then run the same command without `--dry-run` to perform
the deletion:

macOS/Linux:

```bash
.venv/bin/python3 scripts/deprovision_entra_agent_identity.py --agent-user-upn agent@tenant.onmicrosoft.com
```

Windows (PowerShell):

```powershell
.\.venv\Scripts\python.exe scripts\deprovision_entra_agent_identity.py --agent-user-upn agent@tenant.onmicrosoft.com
```

Before deleting anything, the script checks whether another Agent Identity
references the same Blueprint. If one does, the script aborts without
deleting the Agent User's licenses, the Agent User, the Agent Identity, or
the Blueprint — nothing is removed, so other chains sharing that Blueprint
are left intact. Otherwise, it removes, in order: the Agent User's directly
assigned licenses, the Agent User itself, the Agent Identity service
principal, and finally the Blueprint application.

The script does not touch local on-disk state or Azure Blob storage — those
are handled separately. See the
[`deprovision_entra_agent_identity.py` reference](../reference/scripts/teardown/deprovision-entra-agent-identity-py.md).

For cloud storage cleanup, see the
[`deprovision_blob_storage.py` reference](../reference/scripts/storage/deprovision-blob-storage-py.md)
and the [Storage Configuration guide](storage-configuration.md).

## See also

- [Scripts Reference Index](../reference/scripts/index.md)
- [Troubleshooting: Migrations and Upgrades](../troubleshooting/migrations-and-upgrades.md)
