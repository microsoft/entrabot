# Provisioning scripts

Scripts that create and manage the Entra resources behind an Agent Identity chain: Blueprint app, Agent Identity service principal, Agent User, and the dedicated Provisioner app.

All of these use cert-auth tokens minted by `entra_provisioning.py` — never `az` CLI tokens, which include `Directory.AccessAsUser.All` and are rejected by Agent Identity APIs (Learning #1, #4).

## `entra_provisioning.py`

Shared helpers for the dedicated Provisioner app. Mints Graph tokens via a cert whose private key lives in the OS keystore.

### Usage

```bash
# As a library
from entra_provisioning import get_existing_graph_token, get_bootstrap_graph_token

# Standalone bootstrap (creates / rotates the Provisioner app)
python3 scripts/entra_provisioning.py
```

### What it does

- Creates or reuses the `EntraBot Agent ID Provisioner` app registration with `client_credentials` flow.
- Generates a cert pair, stores the private key in Keychain via `keyring`, uploads the public cert to the app.
- Returns Graph access tokens via `get_existing_graph_token()` and `get_bootstrap_graph_token()`.

Pattern matches the Blueprint-cert flow (ADR-003). No client secret on disk.

## `create_entra_agent_ids.py`

Creates the Blueprint + Agent Identity + Agent User chain and persists IDs to `.entrabot-state.json`.

### Usage

```bash
python3 scripts/create_entra_agent_ids.py
```

Set `ENTRABOT_NEW_CHAIN=1` to force creation (skips reuse lookups). `setup.sh --new` sets this for you.

### What it does

- Mints a Provisioner Graph token via `entra_provisioning.get_existing_graph_token()`.
- Creates or reuses the Blueprint app registration via Graph beta `/applications`.
- Creates `BlueprintPrincipal` explicitly (NOT auto-created — Learning #2).
- Creates the Agent Identity service principal via `/servicePrincipals`.
- Creates the Agent User via `/users` and links it to the Agent Identity.
- Grants required `oauth2PermissionGrants` for Graph delegated scopes.

### Idempotency

Each step checks for existing state in `.entrabot-state.json` first, then queries Graph for the named object. Re-runs are safe.

## `add_agent_sponsor.py`

Add a user as a sponsor on the configured Agent Identity.

### Usage

```bash
python3 scripts/add_agent_sponsor.py user@example.com
python3 scripts/add_agent_sponsor.py user@example.com --agent-object-id <OID>
```

### What it does

- Reads `AGENT_OBJECT_ID` from `.entrabot-state.json` (or uses `--agent-object-id`).
- Mints a Graph token via the Provisioner cert.
- Resolves the email to a user object id — works for home-tenant users and B2B guests via `mail` / UPN / `proxyAddresses`.
- POSTs to `/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors/$ref`.
- Prints the updated sponsor list.

Use when `wait_for_sponsor_dm` silently rejects inbound DMs because the operator's resolved guest user is not yet a sponsor.

## `remove_agent_sponsor.py`

Inverse of `add_agent_sponsor.py`.

### Usage

```bash
python3 scripts/remove_agent_sponsor.py user@example.com
python3 scripts/remove_agent_sponsor.py user@example.com --agent-object-id <OID>
```

### What it does

- Resolves the email to a user object id.
- DELETEs `/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors/{sponsor_id}/$ref`.
- Prints the updated sponsor list.

## `assign_agent_user_licenses.py`

Assign Teams and / or Copilot licenses to the Agent User.

### Usage

```bash
# Auto-select best available Teams + Copilot SKUs
python scripts/assign_agent_user_licenses.py

# List available SKUs
python scripts/assign_agent_user_licenses.py --list-available

# Assign a specific SKU
python scripts/assign_agent_user_licenses.py --sku ENTERPRISEPACK
```

### What it does

- Queries `/subscribedSkus` for available SKUs.
- POSTs to `/users/{agent_user_id}/assignLicense`.
- Validates the assignment took.

Extracted from `create_entra_agent_ids.py` for standalone use. The original `assign_license_to_agent_user()` remains in the monolith for `setup.sh` compatibility.

## `remove_agent_user_licenses.py`

Inverse of `assign_agent_user_licenses.py`. Only directly-assigned licenses can be removed; group-inherited licenses are reported but skipped.

### Usage

```bash
python3 scripts/remove_agent_user_licenses.py --all
python3 scripts/remove_agent_user_licenses.py --sku-id <SKU_ID>
python3 scripts/remove_agent_user_licenses.py --all --upn agent@example.com
```

## `ensure_a365_work_iq_permissions.py`

Ensure Microsoft Agent 365 Work IQ MCP tenant resources are materialized before the `a365` CLI runs.

### Usage

```bash
python scripts/ensure_a365_work_iq_permissions.py
```

### What it does

- Uses the Provisioner token to create the first-party resource service principals Work IQ depends on.
- Creates Blueprint-wide `oauth2PermissionGrants` for Work IQ scopes.
- Runs before the `a365` CLI's permission step, which can fail silently with "OAuth2 grants failed" otherwise.

See `docs/platform-learnings/microsoft-agent-365.md` for the A365 identity model.
