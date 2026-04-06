# Hard-Won Learnings

Append-only log of gotchas, surprises, and non-obvious behaviors discovered during development and operations. Never delete entries — mark obsolete ones as `[HISTORICAL]`.

## Active Learnings

### Learning #1: Azure CLI Tokens Rejected by Agent Identity APIs

**Date:** 2026-04-06
**Context:** Running setup.sh to create Agent Identity Blueprint
**Problem:** `az rest` calls to Agent Identity beta APIs returned 403
**Root cause:** Azure CLI tokens always include `Directory.AccessAsUser.All` delegated permission. Agent Identity APIs explicitly reject any token containing this permission.
**Fix:** Created a dedicated "Openclaw Provisioner" app registration. Use `ClientSecretCredential` from `azure-identity` to get a clean `client_credentials` token.
**Prevention:** Never use `az rest` or `DefaultAzureCredential` for Agent Identity APIs. Always use a dedicated app with `client_credentials`.

### Learning #2: BlueprintPrincipal Must Be Created Separately

**Date:** 2026-04-06
**Context:** Creating Agent Identity after Blueprint
**Problem:** Agent Identity creation failed with 400: "The Agent Blueprint Principal for the Agent Blueprint does not exist"
**Root cause:** Creating a Blueprint (`POST /applications`) does NOT auto-create its BlueprintPrincipal (service principal). This is an explicit second step.
**Fix:** Always `POST /servicePrincipals` with `@odata.type: AgentIdentityBlueprintPrincipal` immediately after Blueprint creation. Also check on the skip path (idempotent re-runs).
**Prevention:** Follow the implement-agent-id skill checklist.

### Learning #3: MSAL Returns Error Dicts, Not Exceptions

**Date:** 2026-04-06
**Context:** Bootstrap device code flow
**Problem:** `result["access_token"]` threw KeyError with no context about what went wrong
**Root cause:** MSAL Python returns `{"error": "...", "error_description": "..."}` on failure instead of raising an exception. This is by design (OAuth2 convention).
**Fix:** Check every MSAL result: `if "error" in result: raise MSALError(...)`. Wrapper in `errors.py`.
**Prevention:** Never access `result["access_token"]` without checking for `"error"` first.

### Learning #4: OBO Requires Matching Token Audience

**Date:** 2026-04-06
**Context:** OBO token exchange failing with `invalid_grant`
**Problem:** Device code flow with `scopes=["User.Read"]` produces token with `aud=https://graph.microsoft.com`. OBO exchange requires the incoming token's `aud` to match the app's client ID.
**Root cause:** OBO is designed for middle-tier APIs. The incoming token must be *for* your API, not for Graph.
**Fix:** Expose custom API scope `api://<client-id>/access_as_user` on the Blueprint. Device code flow requests that scope. Token gets `aud=<client-id>`. OBO exchange works.
**Prevention:** Always create a custom API scope for OBO. Document in setup.

### Learning #5: Agent IDs Cannot Have Password Credentials

**Date:** 2026-04-06
**Context:** Trying to create an agent as a regular Entra user with a password
**Problem:** Agent Identities are service principals without backing application objects. `passwordCredentials` returns `PropertyNotCompatibleWithAgentIdentity`.
**Root cause:** Agent IDs are designed for managed identity federation and certificates, not passwords. ROPC flow is fundamentally incompatible.
**Fix:** Use client credentials on the Blueprint (which IS an application) for device-local scenarios. Production uses managed identity + federated credentials.
**Prevention:** Never create "fake users" for agents. Always use the Agent Identity Blueprint → Agent Identity pattern.

### Learning #6: Never Redirect Stderr to /dev/null

**Date:** 2026-04-06
**Context:** Admin consent failure was invisible, token acquisition failure was invisible
**Problem:** `2>/dev/null` hid the actual error messages, turning specific failures into generic "something failed" messages
**Root cause:** Copy-pasted shell patterns from examples that prioritize clean output over debuggability
**Fix:** Removed all 44 instances of `2>/dev/null` from scripts
**Prevention:** Never swallow stderr. Errors must always be visible.

### Learning #7: az CLI JSON Output Safer Than TSV

**Date:** 2026-04-06
**Context:** `az ad app credential reset --query password -o tsv` included Azure CLI WARNING text
**Problem:** The extracted password was corrupted by a WARNING message about protecting credentials
**Root cause:** `-o tsv` outputs to stdout, but Azure CLI also writes warnings to stdout (not stderr) in some cases
**Fix:** Parse full JSON output with Python: `json.loads(output)['password']`
**Prevention:** Use `-o json` and parse with Python/jq, not `-o tsv`.

### Learning #8: Permission Propagation Takes 30-120 Seconds

**Date:** 2026-04-06
**Context:** Token acquisition after admin consent returned cached claims without new permissions
**Problem:** Immediate token acquisition after consent got a token without Agent Identity permissions
**Root cause:** Entra's token endpoint serves cached claims. Even `get_token()` returns cached tokens based on lifetime (usually 1hr).
**Fix:** 10-40s retry backoff + 30s explicit wait after consent. Retry with exponentially increasing delays.
**Prevention:** Always add propagation delay after permission changes.

---

## Historical Learnings

<!-- Move entries here when they no longer apply, with a note about why -->
