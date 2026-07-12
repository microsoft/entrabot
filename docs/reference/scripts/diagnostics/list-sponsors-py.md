# `list_sponsors.py`

## Purpose

Lists the sponsors currently assigned to an Agent Identity — the read counterpart to adding/removing sponsors — so you can confirm who is on the allowlist before troubleshooting a rejected sponsor DM, before removing someone, or as a quick sanity check after provisioning.

## Requirements

- Python 3.12+ with the project's virtualenv active and `entrabot` importable.
- A bootstrapped Provisioner app: like `list_agent_identities.py`, this script calls `get_existing_graph_token()`, which requires the Provisioner app registration and its certificate to already exist (from `setup.sh` or a prior `entra_provisioning.py` bootstrap) — it does not create or repair the Provisioner app.
- An Agent Identity object ID, either passed explicitly or already recorded as `AGENT_OBJECT_ID` in `.entrabot-state.json`.

## Usage

```bash
# Use the Agent Identity recorded in .entrabot-state.json
python scripts/list_sponsors.py

# Explicit Agent Identity object ID
python scripts/list_sponsors.py --agent-object-id <OID>

# Machine-readable output
python scripts/list_sponsors.py --json
```

`--json` prints the raw Graph `value` array (list of sponsor objects with `id`, `displayName`, `userPrincipalName`, `mail`) instead of the fixed-width table, and skips the "no sponsors found" / "Total: N sponsor(s)" summary lines.

## Effects

Read-only. Queries the Microsoft Graph **beta** endpoint `GET /servicePrincipals/{agentObjectId}/microsoft.graph.agentIdentity/sponsors?$select=id,displayName,userPrincipalName,mail`. Nothing is created, modified, or deleted. Note that this endpoint's `$select` only reliably returns `id` — the `displayName`/`userPrincipalName`/`mail` fields commonly come back null here regardless of the projection requested; if you need to understand *why* those fields are empty for a given sponsor, use `diagnose_sponsor_emails.py`, which probes the enrichment path this listing does not attempt.

## Exit behavior

- `1` — the Provisioner token could not be minted (`ProvisionerBootstrapError`), no Agent Identity object ID was available (neither `--agent-object-id` nor state), or the Graph request returned a non-200 status.
- `0` — the query succeeded, including the case where zero sponsors are assigned (an empty allowlist is not treated as an error).

## Related commands

- [`diagnose_sponsor_emails.py`](diagnose-sponsor-emails-py.md) — explains why the `mail`/`userPrincipalName` fields this command prints are frequently null.
- [`add-agent-sponsor.py`](../provisioning/add-agent-sponsor-py.md) and [`remove-agent-sponsor.py`](../provisioning/remove-agent-sponsor-py.md) — the write-side commands that change what this listing shows.
- [`list-agent-identities.py`](list-agent-identities-py.md) — resolve an Agent Identity's object ID first if you don't already have one recorded in state.
- [Microsoft Entra Agent ID — Sponsors](../../../platform-docs/agent-id-blueprints-and-users.md#sponsors) — what a sponsor is and how the relationship is modeled in Graph.
- [Scripts reference: Diagnostics](../index.md#diagnostics) — the other diagnostics in this set.
