# Calling other APIs as the agent

The agent has a standard Microsoft Entra identity: a Blueprint, an Agent Identity and
an Agent User. Its sign-in can get a token for any API that accepts Entra-issued access
tokens, with the same three hops Entrabot already uses for Microsoft Graph, Azure
Storage and Work IQ. Only the third hop's scope changes, to
`api://<the API's application ID>/.default`, through
`entrabot.tools.teams.acquire_agent_user_token(config, resource_scope=...)`.

The API must be registered in the tenant as its own application, expose a delegated
scope, and have a service principal.

## Allowing it, one agent at a time

An administrator grants the API's scope to one agent's Agent User, from that agent's
folder:

```bash
python scripts/grant_consent.py --scopes <scope> --resource-app-id <the API's application ID>
```

This is the same delegated permission grant setup writes for Graph and Storage. It
covers this agent only. To take it back:

```bash
python scripts/revoke_consent.py --all --resource-app-id <the API's application ID>
```

`revoke_consent.py` changes only the grant on the resource it is given (Microsoft Graph
by default), so the agent's Graph and Storage grants stay as they are.

## What was run for real

On Windows 11, on 2026-11-15, with a throwaway agent, against a small stand-in API that
validates Entra tokens properly: the signature against the tenant's keys, the issuer,
the audience, the expiry and the scope. The stand-in is not part of this repository.

- Before the grant, Entra refused the token at the third hop with `AADSTS65001`.
- After the grant to that one agent, the first call got a token and the API answered
  `200`.
- The token's audience was the API's application ID, its scope the granted scope, its
  `preferred_username` the agent's address and its `azp` the Agent Identity's ID.
- It carried none of Microsoft's agent claims (such as `xms_sub_fct`), so an API cannot
  tell from the token alone that the caller is an agent's user account.

Not run on macOS or Linux.

## Limits

- Entrabot ships no tools for other services: its tools are Teams, mail, files and
  Office documents. Your MCP host can load other MCP servers beside it.
- A service that only supports interactive single sign-on in a browser is different:
  an Agent User has no password and cannot sign in interactively.
- Nothing here is a claim about any particular third-party product.

## Related

- [Identity and Token Flow](../architecture/identity-and-token-flow.md)
- [Token Flows reference](../reference/token-flows.md)
- [`grant_consent.py`](../reference/scripts/auth-and-certs/grant-consent-py.md), [`revoke_consent.py`](../reference/scripts/auth-and-certs/revoke-consent-py.md)
