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
powershell -ExecutionPolicy Bypass -File status-windows.ps1 -HealthOnly -Strict
```

A healthy Agent User token has `idtyp=user`, the Agent User's `oid`, and Microsoft Graph as its audience. The Agent Identity and Agent User are separate objects and should both appear in status output.

## Check the MCP wiring

On macOS or Linux, re-run the full diagnostics without provisioning anything:

```bash
./scripts/setup.sh --diagnose
```

It changes nothing. It exits `0` when no check fails and `1` otherwise. It covers the state file, the certificate (in your keychain or keyring, in date, and its key signs; a warning when 30 days or fewer remain), the three-hop token, Graph identity, Teams scope, and the MCP configuration files, and ends with an optional `persona-sati MCP` line. That line reports `SKIP` when `.mcp.json` has no `persona-sati` entry, and never fails the run: a `WARN` means the entry still has an unfilled `<...>` placeholder, points at a launcher or token command that is missing or not executable, or points at a `localhost` address where nothing is listening. A failed local connection does not mean a hosted persona-sati service is down; connect through the client launcher from your persona-sati install, per its own setup documentation. See [MCP connectivity](../troubleshooting/mcp-connectivity.md#persona-sati-does-not-connect).

On Windows, the same checks run with:

```bat
scripts\setup-windows.cmd -Diagnose
```

It changes nothing. It exits `0` when every check passes and `1` otherwise.

## Inspect status directly

```bash
./status.sh
```

This runs `scripts/show_agent_status.py` in the project's environment, and prints the Agent Identity's object ID, the Agent User's UPN, license assignment status, and certificate expiry. See [scripts/show_agent_status.py reference](../reference/scripts/operations/show-agent-status-py.md) for the full output shape.

## Start the MCP host and send your first message

On macOS or Linux, from the Entrabot folder:

```bash
./start-entrabot.sh
```

It runs `claude --dangerously-load-development-channels server:entrabot` there. On Windows, double-click `start-entrabot.cmd` in the Entrabot folder. It runs the same command.

Then ask the host to call `whoami` and send a Teams message. Both should report and act as the Agent User identity, not your human account. See [Identity and Token Flow](../architecture/identity-and-token-flow.md) for how the three-hop token exchange makes this possible.

## If something goes wrong

See [Troubleshooting](../troubleshooting/index.md).
