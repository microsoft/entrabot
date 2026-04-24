# Auth & Token Flows Layer

## Purpose

Handles all interactions with Microsoft Entra ID — three-hop Agent User token acquisition, Agent ID registration, and token lifecycle.

## Three-Hop Flow

The auth layer implements the autonomous Agent User authentication:

1. **Hop 1:** Blueprint → `client_credentials` → Blueprint token
2. **Hop 2:** Agent Identity → FIC exchange (Blueprint token as assertion) → Agent Identity token
3. **Hop 3:** Agent User → `user_fic` grant → delegated user token (`idtyp=user`)

No human in the loop. No MSAL at runtime — uses raw `httpx` calls to the Entra token endpoint.

## Key Files

- `src/entraclaw/tools/teams.py` — `acquire_agent_user_token()` implements the three-hop flow
- `src/entraclaw/config.py` — loads Blueprint credentials and Agent User IDs from `.env`
- `src/entraclaw/errors.py` — `TokenExchangeError` with hop identification for debugging

## Error Handling

Every token response is checked for the `"error"` key before accessing `"access_token"`. The `TokenExchangeError` includes which hop failed (`hop1:blueprint`, `hop2:agent_identity`, `hop3:agent_user`) so you know exactly where the chain broke.

## What Changed (from OBO)

The previous design used MSAL's `PublicClientApplication` + `ConfidentialClientApplication` for device-code → OBO exchange. This was replaced because Agent Users authenticate autonomously — no human token needed. See [ADR-002](../../decisions/002-agent-user-over-obo.md).

Removed: `msal` runtime dependency, `PublicClientApplication`, `ConfidentialClientApplication`, `acquire_token_on_behalf_of`, human refresh token caching, `access_as_user` custom scope.
