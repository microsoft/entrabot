# Microsoft Agent 365 and Work IQ

**Platform status:** Microsoft Agent 365 and Microsoft Entra Agent ID reached GA on 2026-05-01. AI teammate capabilities may still have tenant, region, licensing, or Frontier-program requirements; verify them in the target tenant.

**Entrabot status:** The Agent 365 provider boundary and Work IQ Word adapter are shipped. Entrabot continues to use Microsoft Graph for Teams, Outlook, and Files, and uses Work IQ for Word document content and comments.

## What Agent 365 provides

Microsoft Agent 365 is an enterprise control plane for agents:

1. **Identity and inventory** through Microsoft Entra Agent ID and the Microsoft 365 admin center.
2. **Governance and observability** through Microsoft 365, Purview, Defender, and OpenTelemetry-compatible signals.
3. **Work IQ MCP servers** for Microsoft 365 data and actions.
4. **AI teammate experiences** for agents that have their own Microsoft 365 identity, mailbox, and Teams presence.

Entrabot builds the identity chain directly from Microsoft Graph primitives:

```text
Agent Identity Blueprint
  -> BlueprintPrincipal
  -> Agent Identity
  -> Agent User
```

The Blueprint, BlueprintPrincipal, and Agent Identity creation calls use the dedicated Microsoft Graph v1.0 subtype endpoints. Agent User creation remains on Graph beta. See [Agent ID Blueprints and Users](agent-id-blueprints-and-users.md).

## Current Entrabot integration

| Agent 365 surface | Entrabot state |
|---|---|
| Agent registration and identity | Shipped through direct Graph provisioning. |
| Agent User mailbox and Teams identity | Shipped in `agent_user` mode. |
| Work IQ provider boundary | Shipped in `src/entrabot/a365/`. |
| Work IQ Word create/read/comment/reply | Shipped as MCP tools. |
| Teams, mail, and Files | Remain Graph-native. |
| Agent 365 tenant-wide OTel observability | Not integrated; Entrabot uses its own fail-closed audit log. |

Graph-native Teams is intentional: Entrabot needs explicit chat IDs, background polling, channel push, cross-tenant membership handling, and host-gated sponsor replies. Work IQ is used where it closes a real capability gap, especially Word comments.

## ToolingManifest.json is authoritative

Configure Work IQ servers with the Agent 365 CLI. The generated `ToolingManifest.json` supplies each server's URL, audience, and scope. Runtime code must prefer that manifest over copied documentation values because catalog metadata can change.

Representative manifest shape:

```json
{
  "mcpServers": [
    {
      "mcpServerName": "mcp_WordServer",
      "mcpServerUniqueName": "mcp_WordServer",
      "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
      "scope": "Tools.ListInvoke.All",
      "audience": "{manifest-provided-audience}",
      "publisher": "Microsoft"
    }
  ]
}
```

Entrabot loads the manifest through `src/entrabot/a365/manifest.py`. Missing, malformed, or incomplete manifest entries raise typed errors; the provider does not silently invent credentials or scopes.

## Runtime architecture

`WorkIqProvider` is the audited boundary for Work IQ calls:

```text
MCP tool
  -> typed Word adapter
  -> WorkIqProvider
  -> audience-specific Agent User token
  -> Work IQ MCP client
  -> Microsoft-hosted MCP server
```

The provider:

- resolves server metadata from `ToolingManifest.json`;
- acquires a token for the manifest-provided audience through Entrabot's three-hop Agent User flow;
- calls the Microsoft-hosted MCP server;
- emits pending/success/failure audit events without logging customer arguments or token material.

The implementation lives in:

- `src/entrabot/a365/manifest.py`
- `src/entrabot/a365/tokens.py`
- `src/entrabot/a365/mcp_client.py`
- `src/entrabot/a365/provider.py`
- `src/entrabot/a365/word.py`

## Work IQ Word tools

Entrabot exposes four Word operations through the typed adapter:

| Entrabot operation | Work IQ tool | Purpose |
|---|---|---|
| Read document | `GetDocumentContent` | Returns document HTML and comments. |
| Create document | `CreateDocument` | Creates a `.docx` in the Agent User's Microsoft 365 storage. |
| Add comment | `AddComment` | Adds a top-level Word comment. |
| Reply to comment | `ReplyToComment` | Replies to an existing Word comment. |

Document content is external input. `get_document_content()` wraps returned HTML with the authoritative XPIA boundary before exposing it to the model. Provider raw results remain available for typed parsing and diagnostics but must not be treated as trusted instructions.

Microsoft Graph drive-item `/comments` is not a substitute for Word document comments on `.docx` files. Use Work IQ Word for document-level comments and replies.

## Authentication

Work IQ servers have audience-specific OAuth resources. Entrabot reads the audience from the manifest and requests `{audience}/.default` through the existing Agent User three-hop flow. This preserves Agent User attribution instead of falling back to the human operator.

Do not:

- hard-code a stale Work IQ audience or legacy `McpServers.*` scope;
- turn the Blueprint into an OAuth public client;
- use the signed-in human's delegated token in `agent_user` mode;
- log bearer tokens or complete Work IQ responses.

`ENTRABOT_MODE=delegated` is a separate human-attributed MSAL mode and does not provide Agent User attribution.

## Setup and validation

1. Provision Entrabot's Agent Identity and Agent User.
2. Use the Agent 365 CLI to add the required Work IQ MCP server and grant its permissions.
3. Keep the generated `ToolingManifest.json` in a location discovered by Entrabot or set the configured manifest path.
4. Run the relevant Word MCP tool and confirm the audit event identifies the Agent action.
5. Confirm returned document content has the authoritative external-content envelope.

Use the generated manifest and live tenant consent state as the source of truth. Server availability, license requirements, and admin-center presentation can vary by tenant.

## Related

- [Agent ID Blueprints and Users](agent-id-blueprints-and-users.md)
- [Agent Users](entra-agent-users.md)
- [Token Flows](../reference/token-flows.md)
- [MCP Tool Reference](../reference/api/mcp-tools.md)
- [Hard-Won Learnings](../runbooks/hard-won-learnings.md)
