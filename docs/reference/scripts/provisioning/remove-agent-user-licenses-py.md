# `remove_agent_user_licenses.py`

Removes directly-assigned licenses from the Agent User. Inverse of
[`assign_agent_user_licenses.py`](assign-agent-user-licenses-py.md).

## Purpose

`scripts/remove_agent_user_licenses.py` removes licenses from the Agent User,
either all directly-assigned licenses or one specific SKU. Only
**directly-assigned** licenses can be removed here: licenses inherited from a
group are reported and skipped, because they are governed by the group's
assignment and cannot be removed from the user directly. To drop an inherited
license, change the user's group membership or the group's license assignment.

## Requirements

- **Platform**: macOS, Linux, or Windows.
- **Provisioner app**: a bootstrapped Provisioner registration whose Graph token
  is minted from its certificate.
- **State or override**: `AGENT_USER_UPN` in `.entrabot-state.json`, or an
  explicit `--upn`.
- **Python environment**: the repository virtual environment with `scripts/`
  and `src/entrabot/` importable.

## Usage

```bash
# Remove all directly-assigned licenses
python3 scripts/remove_agent_user_licenses.py --all

# Remove one directly-assigned SKU by SKU id
python3 scripts/remove_agent_user_licenses.py --sku-id <SKU_ID>

# Override the Agent User UPN from state
python3 scripts/remove_agent_user_licenses.py --all --upn agent@example.com
```

### Options

| Option | Description |
| --- | --- |
| `--all` | Remove every directly-assigned license (group-inherited SKUs are skipped). |
| `--sku-id <SKU_ID>` | Remove one license by its SKU id. |
| `--upn <UPN>` | Override the Agent User UPN read from local state. |
| `--help`, `-h` | Print usage and exit. |

One of `--all` or `--sku-id` is required.

## Effects

- Mints a Provisioner Graph token from the certificate in the OS keystore.
- Looks up the Agent User by UPN via `GET /v1.0/users`, selecting
  `assignedLicenses` and `licenseAssignmentStates`.
- Builds the set of group-inherited SKUs from `licenseAssignmentStates` entries
  that carry `assignedByGroup`, prints each as skipped, and excludes them from
  the removal set (including under `--all`).
- Removes the remaining SKUs via `POST /v1.0/users/{id}/assignLicense` with the
  SKU ids in `removeLicenses`.

## Exit behavior

- **Exit 0** — the licenses were removed, or there were no directly-assigned
  licenses to remove (or none assigned at all).
- **Exit 1** — the Provisioner token could not be obtained, no UPN was available
  from state or `--upn`, the Agent User was not found in the directory, or the
  `assignLicense` call failed.
- **Exit 2** — neither `--all` nor `--sku-id` was supplied (usage error).

## Common failures

- **Group-inherited license "won't remove"** — this is expected; inherited
  licenses are reported and skipped. Adjust group membership or the group's
  license assignment instead.
- **Agent User not found** — the UPN in state is stale or the user was already
  removed; pass the correct `--upn`.

## Related commands

- [Provisioning command index](../index.md#provisioning)
- [`assign_agent_user_licenses.py`](assign-agent-user-licenses-py.md)
- [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md)
- Platform docs: [Agent Users](../../../platform-docs/entra-agent-users.md)
  (Licensing)
- Architecture: [Identity and token flow](../../../architecture/identity-and-token-flow.md)
