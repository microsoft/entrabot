# Quickstart

## Prerequisites

- **Python 3.12+** on `PATH`
- **Azure CLI** (`az`) logged in with admin access to your Entra tenant (`Application Administrator` or higher)
- **`gh`** (GitHub CLI) — optional, used by some helper scripts
- **Git**
- **An M365 license** available for the Agent User (E3/E5/Teams Enterprise — anything that grants a Teams seat)
- macOS, Linux, or Windows 10 21H2+/11. Windows uses `scripts/setup-windows.ps1` (PowerShell 7+); see `docs/runbooks/windows-setup.md`.

## One-Command Setup (macOS/Linux)

```bash
./scripts/setup.sh
```

This will:
1. Create a dedicated provisioner app registration (avoids Azure CLI token rejection — Learning #1)
2. Create an Agent Identity Blueprint + BlueprintPrincipal (separate steps — Learning #2)
3. Create an Agent Identity (per-device service principal)
4. Create an Agent User (Entra user account linked to the Agent Identity)
5. Grant consent for Teams/Chat Graph permissions
6. Generate a self-signed certificate, upload public key to Entra, store private key in the OS keystore (Keychain / TPM / Keyring — ADR-003)
7. Install Python dependencies and write `.env` (no secrets — only the cert thumbprint)

The script is **idempotent** — safe to re-run. State is persisted in `.entraclaw-state.json`.

### Optional flags

- `--use-cloud-memory` — opt in to Azure Blob Storage for operational data (interaction log, daily summaries, watched chats, email cursor). Default is local-only.
- `--keep-memory-local` — explicit form of the default. Accepted for backwards compatibility.

See `docs/reference/setup-script.md` for the full flag list, and `docs/guides/storage-configuration.md` for the local-vs-cloud trade-offs.

## After Setup

1. **Assign an M365 license** to the Agent User in the Entra admin center (E3/E5/Teams Enterprise).
2. **Wait 10–15 minutes** for Teams/mailbox provisioning. The Agent User won't be reachable in Teams until this completes — there is no faster path.
3. **Run tests:**

   ```bash
   source .venv/bin/activate
   pytest -v --cov=entraclaw --cov-report=term-missing
   ```

## Common Pitfalls

- **Teams provisioning latency.** The 10–15 min wait is real. If `create_chat` 404s, give it another five minutes before debugging.
- **`az` CLI tokens hard-403 against Agent Identity APIs.** Don't `az rest` against the agent-identity beta — the dedicated provisioner app exists for that reason. Learning #1.
- **`pip install -e .` inside a worktree silently re-points the parent venv.** If you run sub-agents in git worktrees, give them a worktree-local venv. Learning #36.
- **Stderr must stay visible.** Don't `2>/dev/null` setup scripts — failures become invisible. Learning #6.

## Without an Entra Tenant

If you just want to run the code and tests locally:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

All Graph API calls are mocked in tests — no tenant needed.

## Teardown

```bash
./scripts/teardown.sh
```

Removes the Agent User, Agent Identity, Blueprint, Provisioner app, and local state.

## Next Steps

- Read the [System Overview](../architecture/system-overview.md)
- See [Token Flows](../reference/token-flows.md) for auth protocol details
- See [Enforcement Flow](../architecture/enforcement-flow.md) for how a request moves through the system
- Skim [Hard-Won Learnings](../runbooks/hard-won-learnings.md) before changing setup — it captures the 66 things that have already cost engineering hours
