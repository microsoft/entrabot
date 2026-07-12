# `add_agent_sponsor.py`

Resolves an email or UPN to a directory user and adds them as a sponsor on the
configured Agent Identity.

## Purpose

Sponsors are the humans accountable for an Agent Identity, and the sponsor list
gates inbound messaging: when the operator's resolved directory identity is not
a sponsor, the sponsor gate silently rejects their inbound chat messages.
`scripts/add_agent_sponsor.py` is the targeted fix — it resolves an
email/UPN to a user object and adds that user as an **additional** sponsor
(it does not replace the existing list), then prints the updated sponsors so
you can confirm the change took effect.

Sponsors in the Entrabot provisioning convention are always **human users**, not
service principals or groups. The script binds a `/users/{id}` reference, which
matches the platform requirement that a user reference is expected here.

## Requirements

- **Platform**: macOS, Linux, or Windows.
- **Provisioner app**: a bootstrapped Provisioner registration whose Graph token
  is minted from its certificate — never an `az` CLI token, which the Agent
  Identity APIs reject.
- **State**: `AGENT_OBJECT_ID` present in `.entrabot-state.json` (written by
  [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md)). This script has
  no override flag; run provisioning first if the value is missing.
- **Python environment**: the repository virtual environment with `scripts/`
  and `src/entrabot/` importable.

## Usage

```bash
python3 scripts/add_agent_sponsor.py user@example.com
```

Exactly one positional argument is required: the sponsor's email address or UPN.
The Agent Identity object ID is read from `.entrabot-state.json`; there is no
command-line flag to override it.

## Effects

- Mints a Provisioner Graph token from the certificate in the OS keystore.
- Resolves the email/UPN to a directory user object, trying
  `userPrincipalName`, `mail`, `otherMails`, and `proxyAddresses` in turn, so
  both home-tenant users and B2B guests resolve.
- Reads the current sponsor collection via
  `GET /beta/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors`.
- Adds the resolved user via
  `POST /beta/servicePrincipals/{agent}/microsoft.graph.agentIdentity/sponsors/$ref`
  with an `@odata.id` of `/users/{id}`.
- Prints the sponsor list before and after the change, and a reminder to restart
  the Entrabot MCP server so the sponsor gate reloads with the new sponsor.

The only Graph object mutated is the Agent Identity's sponsor navigation
collection; no user, license, or grant is created.

## Exit behavior

- **Exit 0** — the user was added, or was already a sponsor (a `400` naming an
  existing entry is treated as a no-op).
- **Exit 1** — `AGENT_OBJECT_ID` is missing from state, the Provisioner token
  could not be obtained, or the email could not be resolved to a directory user.
- **Exit 2** — no email argument was supplied (usage error).

## Common failures

- **User not resolved** — the address is not present in the agent's home tenant,
  or a B2B guest has not yet been invited. Verify the address and guest status.
- **Sponsor added but the gate still rejects** — restart the MCP server so the
  sponsor gate reloads; the new sponsor is only picked up on reload.
- **`az` CLI token rejected** — ensure the Provisioner certificate token is in
  use rather than an Azure CLI token.

## Related commands

- [Provisioning command index](../index.md#provisioning)
- [`remove_agent_sponsor.py`](remove-agent-sponsor-py.md)
- [`create_entra_agent_ids.py`](create-entra-agent-ids-py.md)
- Platform docs:
  [Agent Identity Blueprints and Users](../../../platform-docs/agent-id-blueprints-and-users.md)
  (Sponsors),
  [Agent Users](../../../platform-docs/entra-agent-users.md)
- Architecture: [Identity and token flow](../../../architecture/identity-and-token-flow.md)
