# `assign_agent_user_licenses.py`

Standalone Agent User license management — auto-selects Teams and Copilot SKUs,
assigns a specific SKU, or lists what is available.

## Purpose

`scripts/assign_agent_user_licenses.py` assigns Microsoft 365 licenses to the
Agent User outside of a full provisioning run. Teams presence requires a
Teams-capable SKU; the Work IQ MCP servers additionally require Microsoft 365
Copilot as a distinct license. This script is the user-facing equivalent of the
license step inside [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md),
extracted so a license can be granted or repaired independently.

## Requirements

- **Platform**: macOS, Linux, or Windows.
- **Provisioner app**: a bootstrapped Provisioner registration whose Graph token
  is minted from its certificate.
- **State**: `AGENT_USER_ID` present in `.entrabot-state.json` (not required for
  `--list-available`).
- **Licenses**: the tenant must hold subscribed SKUs with available seats.
- **Python environment**: the repository virtual environment with `scripts/`
  and `src/entrabot/` importable.

## Usage

```bash
# Auto-select the first available Teams + Copilot SKUs
python scripts/assign_agent_user_licenses.py

# List available Teams/Copilot SKUs and exit
python scripts/assign_agent_user_licenses.py --list-available

# Assign a specific SKU by part number
python scripts/assign_agent_user_licenses.py --sku ENTERPRISEPACK
```

### Options

| Option | Description |
| --- | --- |
| _(none)_ | Auto-select: assign a Teams-capable SKU and a Copilot SKU if the Agent User does not already have each. |
| `--list-available` | Print subscribed SKUs that have available seats, marking Teams- and Copilot-capable part numbers, then exit. |
| `--sku PART_NUMBER` | Assign one specific SKU by part number (for example `ENTERPRISEPACK`). |

## Effects

- Reads available SKUs via `GET /v1.0/subscribedSkus` (only SKUs with remaining
  seats are considered).
- In auto-select mode, checks the Agent User's existing licenses and assigns
  only the missing capability: the first available Teams-capable SKU and/or the
  first available Copilot SKU. The chosen part numbers are recorded to state
  (`AGENT_USER_LICENSE_SKU`, `AGENT_USER_WORK_IQ_LICENSE_SKU`).
- Before any assignment, sets `usageLocation` on the Agent User (retried), which
  Entra requires prior to licensing.
- Assigns via `POST /v1.0/users/{id}/assignLicense` with the selected `skuId`
  (retried).

`--list-available` performs no mutation.

## Exit behavior

- **Exit 0** — a license was assigned, the Agent User already had the requested
  capability, `--list-available` completed, **or** (auto-select mode only) a
  license assignment attempt failed after retries. Auto-select mode always
  returns 0 at the end of its run: a failed `assignLicense` call prints a
  `WARNING` and the affected SKU is simply not recorded to state, but the
  script does not treat it as a run failure. Check the printed output for
  `WARNING` lines, or re-run with `--list-available` / `--sku`, to confirm a
  license actually landed.
- **Exit 1** — the Provisioner token could not be obtained, `AGENT_USER_ID` is
  missing from state, the requested `--sku` has no available seats, no
  subscribed SKUs are available, or `usageLocation` could not be set. In
  `--sku` mode only, a failed `assignLicense` call after retries also exits 1
  (unlike auto-select mode).

## Common failures

- **`--sku` not found** — the part number is not subscribed in the tenant or has
  no free seats. Run `--list-available` to see valid part numbers.
- **No subscribed SKUs with seats** — purchase or free up licenses in the admin
  center, then re-run.
- **`usageLocation` could not be set** — the Agent User may still be
  propagating; retry shortly.

## Related commands

- [Provisioning command index](../index.md#provisioning)
- [`remove_agent_user_licenses.py`](remove-agent-user-licenses-py.md)
- [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md)
- Platform docs: [Agent Users](../../../platform-docs/entra-agent-users.md)
  (Licensing)
- Architecture: [Identity and token flow](../../../architecture/identity-and-token-flow.md)
