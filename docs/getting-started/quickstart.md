# Quickstart

## Prerequisites

- Python 3.12+
- Azure CLI (`az`) logged in with admin access to your Entra tenant
- Git
- An M365 license available for the Agent User (E3/E5/Teams Enterprise)

## One-Command Setup

```bash
./scripts/setup.sh
```

This will:
1. Create a dedicated provisioner app registration (avoids Azure CLI token rejection)
2. Create an Agent Identity Blueprint + BlueprintPrincipal
3. Create an Agent Identity (per-device service principal)
4. Create an Agent User (Entra user account linked to the Agent Identity)
5. Grant consent for Teams/Chat Graph permissions
6. Generate a self-signed certificate, upload public key to Entra, store private key in OS keystore (Keychain/TPM/Keyring)
7. Install Python dependencies and write `.env` (no secrets — only the cert thumbprint)

The script is **idempotent** — safe to re-run. State is persisted in `.entraclaw-state.json`.

## After Setup

1. **Assign an M365 license** to the Agent User in the Entra admin center (E3/E5/Teams Enterprise)
2. **Wait 10-15 minutes** for Teams/mailbox provisioning
3. **Run tests:**

```bash
source .venv/bin/activate
pytest -v --cov=entraclaw --cov-report=term-missing
```

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

Removes the Agent User, Agent Identity, Blueprint, Provisioner app, and all local state.

## Next Steps

- Read the [System Overview](../architecture/system-overview.md)
- See [Token Flows](../reference/token-flows.md) for auth protocol details
- See [Enforcement Flow](../architecture/enforcement-flow.md) for how a request moves through the system
