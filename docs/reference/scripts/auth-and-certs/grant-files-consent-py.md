# grant_files_consent.py

Repair a missing-scope error on a Files or SharePoint MCP tool call by ensuring
the Agent User's delegated consent grant carries the full required Graph scope
set. A thin wrapper that invokes only the consent step of
[`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md).

Part of the [auth-and-certs command reference](../index.md#auth-and-certs).

## Purpose

When a Files MCP tool raises `MissingPermissionError`, it usually means the
Agent User was provisioned before the Files/Sites scopes were added to the
required set. This command calls `grant_agent_user_consent`, which idempotently
ensures the Agent Identity → Agent User `oauth2PermissionGrant` contains the
complete Graph scope set the agent needs, adding only the scopes that are
missing.

The full set it guarantees is:

```
Chat.Create Chat.ReadWrite ChatMessage.Send User.Read User.ReadBasic.All
Files.ReadWrite Files.Read.All Sites.Read.All Sites.ReadWrite.All
Mail.Read Mail.Send
```

These are **delegated scopes** written to `oauth2PermissionGrants` with
`consentType: "Principal"` — not application permissions and not admin consent.
There is no browser flow: the Provisioner certificate authenticates to Graph and
the grant is a per-principal write. Agent Identity service principals have no app
registration and therefore no `requiredResourceAccess` manifest, so admin-consent
URL flows do not apply. See
[Files and SharePoint Graph API](../../../platform-docs/files-graph-api.md) for
the tool surface these scopes unlock.

## Requirements

- `.entrabot-state.json` must contain `AGENT_OBJECT_ID` and `AGENT_USER_ID`. Run
  [`setup.sh`](../setup/setup-sh.md) first if either is missing.
- The Provisioner app must already exist with its certificate in the OS keystore;
  this command uses the existing-only token helper and never bootstraps it.
- The Provisioner needs `DelegatedPermissionGrant.ReadWrite.All`.

## Usage

```bash
python scripts/grant_files_consent.py
```

The command takes no arguments — the scope set is fixed.

## Options

None.

## Effects

1. Reads `AGENT_OBJECT_ID` and `AGENT_USER_ID` from state and prints them.
2. Mints an existing Provisioner Graph token (certificate key read from the OS
   keystore in memory only).
3. Resolves the Microsoft Graph service principal, then finds the existing
   Agent Identity → Agent User grant:
   - **All required scopes present** → prints `[skip]`, no change.
   - **Some missing** → `PATCH` the grant with the union (only missing scopes
     added).
   - **No grant** → `POST` a new grant, retrying up to four times on
     principal-propagation errors.
4. Prints a reminder to restart the `entrabot-mcp` server so the next three-hop
   Agent User token is minted with the patched grant.

## Exit behavior

- `0` — consent ensured (created, patched, or already complete).
- `1` — `AGENT_OBJECT_ID`/`AGENT_USER_ID` missing from state.
- `2` — Provisioner token acquisition failed.
- A hard, non-retryable failure to create the grant is treated as blocking and
  exits `1` from within the shared consent helper.

## Security

The Provisioner token and certificate material never touch disk or logs. This
command only adds the fixed Files/Sites and companion scopes; it never removes
existing scopes.

## Common failures

- **Missing state** — run [`setup.sh`](../setup/setup-sh.md) to provision the
  Agent Identity and Agent User.
- **Scopes granted but tool still fails** — the running MCP server holds a token
  minted under the old grant; reconnect/restart `entrabot-mcp` so a fresh token
  picks up the new scopes.

## Related commands

- [`grant_consent.py`](grant-consent-py.md) — the generalised form that grants
  arbitrary scopes against any resource.
- [`revoke_consent.py`](revoke-consent-py.md) — remove scopes or delete the
  grant.
- [`show_permissions.py`](../operations/show-permissions-py.md) — inspect the
  current delegated grants.
- [`create_entra_agent_ids.py`](../provisioning/create-entra-agent-ids-py.md) —
  the provisioning script whose consent step this wraps.
