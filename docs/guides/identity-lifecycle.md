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

**Windows** operators rotate the Blueprint certificate with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1
```

The certificate in use stays in use until the new one is proven to work:

1. A new certificate is created on this PC, with its key in the TPM where the TPM
   accepts one.
2. Its public half is added to the Blueprint beside the current certificate. Other
   machines' certificates are kept.
3. The agent signs in with the new certificate, retrying while Microsoft Entra takes
   it up.
4. `.env` switches to the new certificate: both the `x5t#S256` (SHA-256) thumbprint
   used in JWT assertion headers and the 40-character SHA-1 thumbprint that locates
   the key in the Windows certificate store.
5. The old certificate is removed from the Blueprint and deleted from this PC together
   with its private key.

A failure in steps 1-3 undoes the new certificate and changes nothing else. A failure
in step 5 leaves the agent working on the new certificate; running the command again
finishes the cleanup. Restart the assistant afterwards, because the agent reads its
certificate when it starts. A certificate is valid for one year, and the agent logs a
warning when 30 days or fewer remain. See the
[`deploy-windows.ps1` reference](../reference/scripts/setup/deploy-windows-ps1.md).

To disconnect a Windows PC from an agent, run `scripts\teardown-windows.ps1`. It
removes that PC's certificate from the Blueprint, keeping other machines', and deletes
it locally with its key. It also retires that PC's setup (Provisioner) certificate
(removed from the Provisioner app and deleted locally with its key) unless another
Entrabot agent on the PC still needs it or `-KeepSetupCredential` is given. See the
[`teardown-windows.ps1` reference](../reference/scripts/teardown/teardown-windows-ps1.md).

**macOS and Linux** have no automated rotation flow. Setup checks the certificate on
every run instead. `find_local_blueprint_cert.py` tests whether this machine's private
key matches a certificate registered on the Blueprint that hasn't expired, and setup
reuses that certificate if so, correcting a stale thumbprint in local state. Otherwise
(an expired certificate counts as no match, so the next run adds a new one), and whenever a new
key from an unfinished run is still pending, setup runs `register_blueprint_cert.py` to
add one. It never trusts the cached thumbprint on its own. If the check itself fails
(exit `3`), setup stops rather than generating a certificate, so fix the cause first. To
ask whether one particular thumbprint, such as the one in `.env`, is still on the
Blueprint, use `verify_blueprint_cert.py`; setup does not call it. See the
auth-and-certs reference pages for
[`find_local_blueprint_cert.py`](../reference/scripts/auth-and-certs/find-local-blueprint-cert-py.md),
[`register_blueprint_cert.py`](../reference/scripts/auth-and-certs/register-blueprint-cert-py.md),
and
[`verify_blueprint_cert.py`](../reference/scripts/auth-and-certs/verify-blueprint-cert-py.md).

### Several machines on one Blueprint

A Blueprint holds one certificate per machine, and attaching a machine with
`setup.sh --use-blueprint=<APP_ID>` adds to the list without removing any other
machine's certificate. See which certificates are registered with
[`list_blueprint_certs.py`](../reference/scripts/auth-and-certs/list-blueprint-certs-py.md).

To start over, for example after losing a laptop,
`setup.sh --use-blueprint=<APP_ID> --remove-all-existing-certs` removes **every**
certificate on the Blueprint and replaces them with a new one for the machine you run
it on. Every other machine stops authenticating until it runs setup again. The flag
only removes certificates: it does not delete the Blueprint, its Agent Identities, or
its Agent Users. To delete those, see [Targeted deprovisioning](#targeted-deprovisioning).
See the
[`register_blueprint_cert.py` reference](../reference/scripts/auth-and-certs/register-blueprint-cert-py.md).

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
