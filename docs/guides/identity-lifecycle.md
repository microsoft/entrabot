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
pwsh -File scripts/deploy-windows.ps1
```

This wraps `rotate_cert_windows.py`: it captures the current certificate's
public DER before generating a new one (the only chance to do so for
non-exportable TPM-backed keys), swaps the new public certificate onto the
Blueprint app in place of the old one while keeping other machines'
certificates, and updates both thumbprints it tracks: the `x5t#S256`
(SHA-256) used in JWT assertion headers, and the 40-character SHA-1
thumbprint Windows CNG uses to locate the private key in the certificate
store. It then runs a smoke test against the new certificate, and rolls back
the PATCH plus local state if the smoke test fails. See the
[`deploy-windows.ps1` reference](../reference/scripts/setup/deploy-windows-ps1.md)
and the
[`rotate_cert_windows.py` reference](../reference/scripts/auth-and-certs/rotate-cert-windows-py.md).

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
