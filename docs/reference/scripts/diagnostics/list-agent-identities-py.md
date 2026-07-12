# `list_agent_identities.py`

## Purpose

Lists the Agent Identity service principals that belong to a given Blueprint, so you can find the right Agent Identity's object ID or app ID when a tenant has more than one Agent Identity chain (or more than one Blueprint) and you need to disambiguate before pointing another command at a specific one.

## Requirements

- Python 3.12+ with the project's virtualenv active and `entrabot` importable.
- A bootstrapped Provisioner app: the script calls `get_existing_graph_token()`, which mints a token from the Provisioner's certificate but does **not** create or repair the Provisioner app registration if it's missing. Run `setup.sh` (or `entra_provisioning.py`'s standalone bootstrap) first if this is a fresh machine.
- A Blueprint app ID, either passed explicitly or already recorded as `BLUEPRINT_APP_ID` in `.entrabot-state.json`.

## Usage

```bash
# Use the Blueprint recorded in .entrabot-state.json
python scripts/list_agent_identities.py

# Explicit Blueprint app ID
python scripts/list_agent_identities.py --blueprint-app-id <APP_ID>
```

`--blueprint-app-id` is the only flag. There is no `--json` mode for this command — output is a fixed-width table.

## Effects

Read-only. The script queries the Microsoft Graph **beta** endpoint `GET /servicePrincipals/microsoft.graph.agentIdentity?$select=id,appId,displayName,agentIdentityBlueprintId&$top=999`, then filters the results client-side by matching `agentIdentityBlueprintId` against the resolved Blueprint app ID (Graph does not support a server-side filter on that field for this collection). Nothing is created, modified, or deleted. Output is a table of display name, app ID, and object ID for each matching Agent Identity, followed by a total count.

## Exit behavior

- `1` — the Provisioner token could not be minted (`ProvisionerBootstrapError`), no Blueprint app ID was available (neither `--blueprint-app-id` nor state), or the Graph request itself failed.
- `0` — the query ran successfully, including the case where zero Agent Identities matched the Blueprint (an empty result is not treated as an error).

## Related commands

- [`list_sponsors.py`](list-sponsors-py.md) — the equivalent listing one level down: sponsors of a specific Agent Identity rather than Agent Identities under a Blueprint.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) — creates the Agent Identities this command lists.
- [`provisioner-token.py`](../auth-and-certs/provisioner-token-py.md) — mints the same class of Provisioner Graph token this script uses, useful for manually re-running the same query with different parameters.
- [Microsoft Entra Agent ID — Object model](../../../platform-docs/agent-id-blueprints-and-users.md#object-model) — how Blueprints, BlueprintPrincipals, and Agent Identities relate.
- [Scripts reference: Diagnostics](../index.md#diagnostics) — the other diagnostics in this set.
