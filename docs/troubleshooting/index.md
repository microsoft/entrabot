# Troubleshooting

Start with the symptom, then use the narrowest diagnostic that can separate
configuration, authentication, transport, and Microsoft 365 provisioning
problems.

## Choose the matching topic

| Symptom | Go to |
|---|---|
| Setup fails, sign-in loops, token exchange names a hop, or the runtime is unauthenticated | [Setup and authentication](setup-and-authentication.md) |
| Teams messages do not send or arrive, email is missing, or Graph returns 401/403/404/429 | [Teams and email](teams-and-email.md) |
| PowerShell, certificate-store, TPM, path, or Windows data-directory problem | [Windows](windows.md) |
| Blob selection, storage consent, RBAC, migration, or cursor-concurrency problem | [Storage](storage.md) |
| MCP server will not start, disconnects, or channel push is missing | [MCP connectivity](mcp-connectivity.md) |
| An upgrade changed state, credentials, cursors, or local paths | [Migrations and upgrades](migrations-and-upgrades.md) |

## Run the right identity check

Two diagnostics answer different questions:

- **Status and health commands** use the Provisioner application's certificate
  to inspect the Blueprint, Agent Identity, Agent User, grants, licenses,
  certificates, and storage configuration. They do not prove that the running
  MCP process has an Agent User token.
- **`whoami`** is an MCP tool. It uses the runtime session and reports the
  active identity state, authentication mode, attribution type, and Graph
  identity. Use it to verify the process that is serving tools.

Run status and health:

=== "macOS or Linux"

    ```bash
    ./status.sh --health-only --strict
    ```

=== "Windows"

    ```powershell
    .\status-windows.ps1 -HealthOnly -Strict
    ```

Then call `whoami` from the connected MCP host. A healthy Provisioner status
with a failing `whoami` points to runtime authentication or MCP configuration,
not necessarily a broken Entra resource chain.

## Collect safe diagnostics

1. Record the command, exit code, HTTP status, and named token-exchange hop.
2. Check `entrabot.log` under `ENTRABOT_LOG_DIR` or its platform default.
3. Confirm paths and non-secret identifiers in `.mcp.json`, `.env`, and
   `.entrabot-state.json`.
4. Do not paste access tokens, client assertions, private keys, device codes, or
   complete credential files into an issue.

See [Configuration Reference](../reference/configuration.md),
[Token Flows](../reference/token-flows.md), and
[Scripts Reference](../reference/scripts/index.md) for exact interfaces.
