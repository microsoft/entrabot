# Verify Your Agent Identity

## What gets created in Entra

1. **Agent Identity Blueprint** — a certificate-backed app registration that represents the *kind* of agent (e.g., "entrabot on this device").
2. **BlueprintPrincipal** — a service principal for the Blueprint. Not auto-created by Entra; the setup script creates it explicitly.
3. **Agent Identity** — a federated-identity-credential (FIC) child of the Blueprint, representing this specific agent instance.
4. **Agent User** — a real Entra user object with a Teams and Outlook license, linked to the Agent Identity via a `user_fic` grant. This is the identity your agent authenticates as.

## Run the health check

```bash
./status.sh --health-only --strict
```

On Windows:

```powershell
pwsh -File status-windows.ps1 -HealthOnly -Strict
```

A healthy Agent User token has `idtyp=user`, the Agent User's `oid`, and Microsoft Graph as its audience. The Agent Identity and Agent User are separate objects and should both appear in status output.

## Inspect status directly

```bash
python3 scripts/show_agent_status.py
```

This prints the Agent Identity's object ID, the Agent User's UPN, license assignment status, and certificate expiry. See [scripts/show_agent_status.py reference](../reference/scripts/operations/show-agent-status-py.md) for the full output shape.

## Start the MCP host and send your first message

```bash
claude --dangerously-load-development-channels server:entrabot
```

Then ask the host to call `whoami` and send a Teams message. Both should report and act as the Agent User identity, not your human account. See [Identity and Token Flow](../architecture/identity-and-token-flow.md) for how the three-hop token exchange makes this possible.

## If something goes wrong

See [Troubleshooting](../troubleshooting/index.md).
