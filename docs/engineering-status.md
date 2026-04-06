# Openclaw Identity Research — Engineering Summary

**Date:** April 6, 2026
**Team:** Brandon Werner
**Status:** MCP server built (66 tests passing), setup script at Step 11 of 23 — provisioner token acquisition now fixed, Agent Identity creation next

---

## What We're Building

A proof-of-concept demonstrating that **device-local AI agents can have their own identity** in Microsoft Entra, separate from the human user. When Copilot CLI runs on your Mac or Windows machine, it gets an Agent ID via OBO token exchange — so Entra sign-in logs show the **agent** as the actor, not the human.

**Identity Chain:** Human (device code flow) → Entra Agent Identity Blueprint → OBO Token Exchange → Agent-attributed Graph API calls → Teams messaging as agent

### The Demo Scenario

| Step | What Happens | What It Proves |
|------|-------------|----------------|
| 1. `./scripts/setup.sh` | Creates Provisioner app, Agent Blueprint, Agent Identity, caches human token | Proper Entra Agent ID provisioning works on device |
| 2. `copilot` (with Openclaw MCP) | MCP server starts, acquires OBO token silently | Agent can authenticate without human re-consenting |
| 3. `openclaw_teams_send` | Agent sends message to human in Teams | Agent has working Teams integration |
| 4. Check Entra sign-in logs | `azp` claim shows agent's app ID, not human's | **Identity attribution works** — the research goal |

### MCP Tools (4 total)

| Tool | Purpose | Status |
|------|---------|--------|
| `openclaw_whoami` | Show agent identity, sponsor, scopes | ✅ Built + tested |
| `openclaw_teams_send` | Send message to human in Teams | ✅ Built + tested |
| `openclaw_teams_read` | Read human's replies from Teams | ✅ Built + tested |
| `openclaw_audit_log` | Record audit event (action, resource, outcome) | ✅ Built + tested |

---

## Current State

### What Works
- ✅ 988 lines of application code across 15 Python modules
- ✅ 779 lines of tests (66 tests, all passing)
- ✅ 822-line setup.sh with 23-step Entra provisioning (Steps 1-10 verified working)
- ✅ Proper Agent Identity Blueprint + BlueprintPrincipal + Agent Identity creation sequence
- ✅ Dedicated Provisioner app (avoids Azure CLI's Directory.AccessAsUser.All rejection)
- ✅ OBO token flow designed and tested (mocked)
- ✅ Teams Graph API integration (send, read, connect) designed and tested (mocked)
- ✅ Cross-platform credential storage (Keychain on Mac, Credential Manager on Windows)
- ✅ Structured JSON audit logging
- ✅ All code passes ruff lint + format

### What's In Progress
- 🔄 setup.sh Steps 11-23 — provisioner token works now, need to run through Blueprint + Agent Identity creation live
- 🔄 End-to-end test: human device code flow → OBO exchange → Teams message

### What's Not Started
- ❌ Windows VM provisioning and testing
- ❌ AppContainer sandbox spike
- ❌ Token auto-refresh (P1 TODO)
- ❌ Graph API rate limit handler (P2 TODO)

---

## Bugs Encountered & Resolved

### Bug 1: MSAL Error Dict Pattern (Design Review)
**Impact:** Critical — silent crashes with KeyError
**Problem:** MSAL returns error dicts `{"error": "...", "error_description": "..."}` instead of raising exceptions. Code that does `result["access_token"]` throws a KeyError with no context.
**Fix:** Wrapper function checks every MSAL result for `"error"` key before accessing other fields. Full error hierarchy in `src/openclaw/errors.py`.
**Prevention:** Added to CLAUDE.md as a non-negotiable pattern.

### Bug 2: OBO Audience Mismatch (Outside Voice Review)
**Impact:** Critical — bootstrap would fail with `invalid_grant`
**Problem:** Device code flow with `scopes=["User.Read"]` produces a token with `aud=https://graph.microsoft.com`. OBO exchange requires `aud=<your-app-client-id>`. These don't match.
**Fix:** App registration exposes custom API scope `api://<client-id>/access_as_user`. Device code flow requests THAT scope so `aud` matches.
**Prevention:** Documented in tenant setup doc and bootstrap pseudocode.

### Bug 3: Fake User Account Instead of Agent ID (Architecture Error)
**Impact:** Critical — fundamentally wrong identity model
**Problem:** First implementation created a regular Entra user (`openclaw-agent@werner.ac`) with a password and used ROPC flow. This is NOT how Agent IDs work — they're service principals, not users, and can't have passwords.
**Fix:** Complete rewrite to use Agent Identity Blueprint → BlueprintPrincipal → Agent Identity, following the agent-foundry-poc reference implementation.
**Prevention:** Imported `implement-agent-id` skill with all pitfalls documented.

### Bug 4: Azure CLI Tokens Rejected by Agent Identity APIs
**Impact:** Critical — setup.sh Step 11 failed with 403
**Problem:** Azure CLI tokens include `Directory.AccessAsUser.All` delegated permission. Agent Identity APIs explicitly reject any token with this permission.
**Fix:** Dedicated "Openclaw Provisioner" app registration with `client_credentials` flow via `ClientSecretCredential` from `azure-identity`.
**Prevention:** Documented in implement-agent-id skill.

### Bug 5: BlueprintPrincipal Not Auto-Created
**Impact:** Critical — Agent Identity creation would fail with 400
**Problem:** Creating an Agent Identity Blueprint (`POST /applications`) does NOT auto-create its BlueprintPrincipal (SP). Without it, all Agent Identity creation fails.
**Fix:** Explicit `POST /servicePrincipals` with `@odata.type: AgentIdentityBlueprintPrincipal` immediately after Blueprint creation.
**Prevention:** Documented in implement-agent-id skill.

### Bug 6: Admin Consent Silently Swallowed
**Impact:** High — `AADSTS65001` at runtime, 30 min debugging
**Problem:** setup.sh used `az ad app permission admin-consent --id ... 2>/dev/null || true` which hid the actual error.
**Fix:** Retry 3 times with 5s delay, show actual error output. Later: removed ALL `2>/dev/null` from scripts.
**Prevention:** Never redirect stderr to /dev/null in scripts.

### Bug 7: Secret Extraction Corrupted by WARNING Text
**Impact:** High — setup.sh Step 10-11 failed
**Problem:** `az ad app credential reset --query password -o tsv` sometimes includes Azure CLI's WARNING about protecting credentials in the output.
**Fix:** Parse full JSON output with Python, fallback to `--query -o tsv`.
**Prevention:** Always parse `az` output as JSON, not tsv.

### Bug 8: azure-identity Installed to Wrong Python
**Impact:** High — `ModuleNotFoundError: No module named 'azure'`
**Problem:** `$PYTHON -m pip install azure-identity` installed to system Python, not the project `.venv`.
**Fix:** Detect `.venv` and use `.venv/bin/python3` for pip install and token acquisition.
**Prevention:** Always use venv Python for pip operations.

### Bug 9: Permission Propagation Too Fast
**Impact:** Medium — intermittent 403 on token acquisition
**Problem:** After admin consent, Entra's token endpoint may serve cached claims for 30-120 seconds.
**Fix:** Changed from 5s retry to 10-40s backoff + 30s explicit propagation wait.
**Prevention:** Documented in implement-agent-id skill.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Local Device (Mac / Windows)                           │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Copilot CLI (MCP Client)                         │   │
│  │   └── connects via stdio ──┐                     │   │
│  └────────────────────────────┼─────────────────────┘   │
│                               │                         │
│                               ▼                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │ Openclaw MCP Server (Python)                     │   │
│  │                                                  │   │
│  │  openclaw_whoami ──────▶ cached state            │   │
│  │  openclaw_teams_send ──▶ Graph API (OBO token)   │   │
│  │  openclaw_teams_read ──▶ Graph API (OBO token)   │   │
│  │  openclaw_audit_log ───▶ ~/.openclaw/audit/      │   │
│  │                                                  │   │
│  │  Credentials: OS keychain (keyring)              │   │
│  │  Token: OBO (agent-attributed, via Blueprint)    │   │
│  └──────────────────────────────────────────────────┘   │
└───────────┬──────────────────────────┬──────────────────┘
            │                          │
            ▼                          ▼
    ┌───────────────┐          ┌──────────────┐
    │ Entra ID      │          │ Graph API    │
    │ Agent IDs     │          │ Teams Chat   │
    │ OBO Tokens    │          │ Messaging    │
    └───────────────┘          └──────────────┘
```

---

## Key Learnings

1. **Agent IDs are service principals, not users.** No passwords, no ROPC. Use managed identity federation or client credentials on the Blueprint.
2. **Azure CLI tokens are toxic for Agent APIs.** `Directory.AccessAsUser.All` causes hard 403. Must use a dedicated provisioner app with `client_credentials`.
3. **BlueprintPrincipal is a separate creation step.** Not auto-created. Must be created explicitly or Agent Identity creation fails.
4. **Permission propagation takes 30-120 seconds.** Don't retry at 5s intervals. Use 10-40s backoff + explicit wait.
5. **MSAL returns error dicts, not exceptions.** Every single MSAL call must check for `"error"` key.
6. **OBO requires matching `aud` claim.** Device code flow must request your app's custom scope, not Graph scopes directly.
7. **Never swallow stderr.** Every `2>/dev/null` is a future debugging nightmare.
8. **`az` CLI JSON output is safer than `-o tsv`.** TSV can be corrupted by warnings, progress indicators, or multi-line values.

---

## Next Steps

1. **Run setup.sh end-to-end** — Steps 11-23 should now work with the provisioner token fix
2. **Test in Copilot CLI** — verify MCP tools work, Teams message appears
3. **Check Entra sign-in logs** — confirm `azp` claim shows agent identity
4. **Provision Windows VM** — `scripts/setup.sh` should work there too
5. **AppContainer sandbox spike** — kernel-level sandboxing on Windows
6. **Token auto-refresh** — handle 60-90 min token expiry gracefully
