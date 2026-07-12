# `remove_agent_sponsor.py`

Resolves an email or UPN to a directory user and removes them from the Agent
Identity's sponsor list. Inverse of
[`add_agent_sponsor.py`](add-agent-sponsor-py.md).

## Purpose

`scripts/remove_agent_sponsor.py` takes a human sponsor off an Agent Identity.
It resolves the supplied email/UPN to a directory user, deletes that user's
sponsor reference, and prints the remaining sponsors for verification. As with
adding a sponsor, the Entrabot provisioning convention treats sponsors as human
users, and the script operates on a `/users/{id}` reference in the sponsor
navigation collection.

## Requirements

- **Platform**: macOS, Linux, or Windows.
- **Provisioner app**: a bootstrapped Provisioner registration whose Graph token
  is minted from its certificate — never an `az` CLI token, which the Agent
  Identity APIs reject.
- **State or override**: `AGENT_OBJECT_ID` in `.entrabot-state.json`, or an
  explicit `--agent-object-id`.
- **Python environment**: the repository virtual environment with `scripts/`
  and `src/entrabot/` importable.

## Usage

```bash
python3 scripts/remove_agent_sponsor.py user@example.com
python3 scripts/remove_agent_sponsor.py user@example.com --agent-object-id <AGENT_OBJECT_ID>
```

### Options

| Argument | Required | Description |
| --- | --- | --- |
| `email` (positional) | yes | The sponsor's email address or UPN. |
| `--agent-object-id <id>` | no | Override the Agent Identity object ID read from `.entrabot-state.json`. |

## Effects

- Mints a Provisioner Graph token from the certificate in the OS keystore.
- Resolves the email/UPN to a directory user object (by `userPrincipalName`,
  `mail`, `otherMails`, or `proxyAddresses`).
- Reads the current sponsor collection via
  `GET /beta/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors`.
- Removes the resolved user via
  `DELETE /beta/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors/{sponsor_id}/$ref`.
- Prints the sponsor list before and after the change, and a reminder to restart
  the Entrabot MCP server so the sponsor gate reloads.

The only Graph object mutated is the Agent Identity's sponsor navigation
collection.

## Exit behavior

- **Exit 0** — the sponsor reference was deleted, or was already absent (a `404`
  is reported as "not a sponsor" and treated as success).
- **Exit 1** — `AGENT_OBJECT_ID` is missing and no `--agent-object-id` was
  passed, the Provisioner token could not be obtained, the email could not be
  resolved, or the delete failed with an unexpected status.
- **Exit 2** — no email argument was supplied (usage error).

## Common failures

- **User not resolved** — the address is not present in the tenant; verify the
  address and any guest invitation.
- **`az` CLI token rejected** — ensure the Provisioner certificate token is in
  use rather than an Azure CLI token.

## Related commands

- [Provisioning command index](../index.md#provisioning)
- [`add_agent_sponsor.py`](add-agent-sponsor-py.md)
- [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md)
- Platform docs:
  [Agent Identity Blueprints and Users](../../../platform-docs/agent-id-blueprints-and-users.md)
  (Sponsors),
  [Agent Users](../../../platform-docs/entra-agent-users.md)
- Architecture: [Identity and token flow](../../../architecture/identity-and-token-flow.md)
