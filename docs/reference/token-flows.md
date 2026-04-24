# Token Flows Reference

## Overview

Entraclaw uses the **three-hop Agent User flow** for all Graph API calls. This is a fully autonomous machine-to-machine flow — no human authentication, no device-code prompts, no OBO exchange.

The result is a delegated token with `idtyp=user` that can call any Graph API requiring user context (Teams, Exchange, OneDrive).

## The Three Hops

### Hop 1: Blueprint Token (client_credentials)

The Blueprint authenticates with its own client secret (dev) or certificate/FIC (production).

```http
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token

client_id={blueprint-app-id}
&scope=https://graph.microsoft.com/.default
&grant_type=client_credentials
&client_secret={blueprint-secret}
```

**Result:** An application token for the Blueprint. Used as the `client_assertion` in Hop 2.

### Hop 2: Agent Identity Token (FIC exchange)

The Agent Identity authenticates by presenting the Blueprint token. This works because Agent Identities are parented by the Blueprint — the Blueprint's token is trusted as a credential.

```http
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token

client_id={agent-identity-id}
&scope=api://AzureADTokenExchange/.default
&grant_type=client_credentials
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion={blueprint-token}
```

**Result:** A token representing the Agent Identity. Used as the `user_federated_identity_credential` in Hop 3.

### Hop 3: Agent User Token (user_fic grant)

The final hop produces a delegated user token for the Agent User.

```http
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token

client_id={agent-identity-id}
&scope=https://graph.microsoft.com/.default
&grant_type=user_fic
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion={blueprint-token}
&user_id={agent-user-object-id}
&user_federated_identity_credential={agent-identity-token}
```

**Result:** A delegated token with `idtyp=user`. The Agent User is the principal. Can call Teams, Exchange, OneDrive, etc.

## Consent

Before Hop 3 works, an `oAuth2PermissionGrant` must exist granting the Agent Identity permission to act as the Agent User:

```http
POST https://graph.microsoft.com/v1.0/oauth2PermissionGrants

{
  "clientId": "{agent-identity-object-id}",
  "consentType": "Principal",
  "principalId": "{agent-user-object-id}",
  "resourceId": "{ms-graph-sp-object-id}",
  "scope": "Chat.Create Chat.ReadWrite ChatMessage.Send User.Read"
}
```

This is a one-time admin operation, handled by `create_entra_agent_ids.py` during setup.

## Why Not OBO?

The previous design used On-Behalf-Of (OBO) token exchange:

```
Human (device-code flow) → human token → OBO exchange → agent-attributed token
```

This was replaced because:
1. **Device-code flow requires a human** — can't run headless or in CI
2. **Refresh tokens in the keychain** — security risk, expiry management overhead
3. **MSAL runtime dependency** — complex library for a simple token exchange
4. **Agent Users are purpose-built** for this exact scenario — they exist so agents don't need OBO

See [ADR-002](../decisions/002-agent-user-over-obo.md) for the full rationale.
