# Identity Lifecycle and Deprovisioning

This guide covers the resource chain EntraBot provisions in Entra, the
runtime identity states the MCP server moves through, and how to inspect,
rotate, and tear down that chain.

## Provisioning order

Provisioning creates four resources, in order:

1. **Blueprint application** — the Agent Identity Blueprint app registration.
2. **BlueprintPrincipal** — created explicitly as its own step; it is not
   auto-created alongside the Blueprint application.
3. **Agent Identity** — a service principal scoped to the Blueprint.
4. **Agent User** — a real Entra user account linked to the Agent Identity,
   which is what gives the agent Teams presence and a `idtyp=user` token.

See the
[`create_entra_agent_ids.py` reference](../reference/scripts/provisioning/create-entra-agent-ids-py.md)
for the script that runs this chain.

## Runtime identity states

The MCP server's identity state machine is not a strict linear progression
— it defines a specific set of allowed transitions, enforced under an
`asyncio.Lock`:

| From | Allowed transitions to |
|------|------------------------|
| `UNAUTHENTICATED` | `DELEGATED`, `AGENT_USER` |
| `DELEGATED` | `PROVISIONING`, `UNAUTHENTICATED` |
| `PROVISIONING` | `AGENT_USER`, `ERROR`, `DELEGATED` |
| `AGENT_USER` | `ERROR`, `UNAUTHENTICATED` |
| `ERROR` | `DELEGATED`, `UNAUTHENTICATED` |

Every transition acquires the lock, validates it's in this table, and runs
an optional callback inside the lock. If the callback raises, the session
state is rolled back to the snapshot taken when the lock was acquired —
a failed transition doesn't leave partially-updated session state behind.

To inspect the current state and health of the chain — Blueprint, Agent
Identity, Agent User, sponsors, permissions, certificates, licenses, and
storage configuration — run:

```bash
python3 scripts/show_agent_status.py
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
non-exportable TPM-backed keys), PATCHes the new public certificate onto the
Blueprint app, and updates both thumbprints it tracks: the `x5t#S256`
(SHA-256) used in JWT assertion headers, and the 40-character SHA-1
thumbprint Windows CNG uses to locate the private key in the certificate
store. It then runs a smoke test against the new certificate, and rolls back
the PATCH plus local state if the smoke test fails. See the
[`deploy-windows.ps1` reference](../reference/scripts/setup/deploy-windows-ps1.md)
and the
[`rotate_cert_windows.py` reference](../reference/scripts/auth-and-certs/rotate-cert-windows-py.md).

**macOS and Linux** operators use two diagnostic scripts rather than an
automated rotation flow: `find_local_blueprint_cert.py` recovers a
registered certificate thumbprint matching the local private key when local
state is missing it, and `verify_blueprint_cert.py` checks whether a cached
thumbprint is still registered on the Blueprint app. If either script
reports a stale or missing certificate, re-run setup to regenerate and
re-register it. See the auth-and-certs reference pages for
[`find_local_blueprint_cert.py`](../reference/scripts/auth-and-certs/find-local-blueprint-cert-py.md)
and
[`verify_blueprint_cert.py`](../reference/scripts/auth-and-certs/verify-blueprint-cert-py.md).

## Targeted deprovisioning

To remove a single Agent User chain, first run a dry run — the target UPN is
required:

```bash
python3 scripts/deprovision_entra_agent_identity.py --agent-user-upn agent@tenant.onmicrosoft.com --dry-run
```

Review the output, then run the same command without `--dry-run` to perform
the deletion.

Before deleting anything, the script runs
`ensure_blueprint_has_no_other_agent_identities` to check whether another
Agent Identity references the same Blueprint. If one does, the script aborts
without deleting the Agent User's licenses, the Agent User, the Agent
Identity, or the Blueprint — nothing is removed, so other chains sharing that
Blueprint are left intact. Otherwise, it removes, in order: the Agent User's
directly assigned licenses, the Agent User itself, the Agent Identity service
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
