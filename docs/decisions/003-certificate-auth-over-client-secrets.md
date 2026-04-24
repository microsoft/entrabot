# ADR-003: Certificate Auth Over Client Secrets for Device-Local Agents

**Date:** 2026-04-06
**Status:** Proposed
**Deciders:** Brandon Werner
**Context:** Eliminating client secrets from the device-local agent authentication flow

## Context

The current three-hop Agent User flow requires a Blueprint client secret stored in `.env` on the developer's machine. This is:
- A plaintext secret on disk (even with `chmod 600`)
- Manual to rotate (generate new secret in Entra portal, update `.env`)
- A setup friction point (setup.sh must create the secret and write it)
- Explicitly warned against by Microsoft for production use

We investigated whether a human user's interactive token could replace `client_credentials` in Hop 1 to eliminate the secret entirely.

## Decision

**Replace client_secret with certificate-based auth. Do NOT attempt human-token bootstrapping.**

### Why Human Token Bootstrapping Is Impossible

Research confirmed these architectural constraints:

1. **All agent entities are confidential clients.** Microsoft states: "Interactive flows aren't supported for any agent entity type, ensuring that all authentication occurs through programmatic token exchanges rather than user interaction."
2. **Audience chain validation.** Hop 2 validates T1's audience matches the Agent Identity's parent Blueprint. A human token has a different audience and fails validation.
3. **Grant type lock.** Hop 1 is always `grant_type=client_credentials`. No `device_code` or `authorization_code` path exists.
4. **No redirect URIs.** Agent entities explicitly do not support redirect URIs (required for interactive auth).

### Certificate Auth: The Production Path

Microsoft explicitly recommends certificates over secrets. The change is a drop-in replacement for Hop 1:

**Before (client secret):**
```
POST /oauth2/v2.0/token
client_id=<blueprint-app-id>
&scope=api://AzureADTokenExchange/.default
&grant_type=client_credentials
&client_secret=<plaintext-secret>
&fmi_path=<agent-identity-client-id>
```

**After (certificate):**
```
POST /oauth2/v2.0/token
client_id=<blueprint-app-id>
&scope=api://AzureADTokenExchange/.default
&grant_type=client_credentials
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion=<JWT-signed-by-private-key>
&fmi_path=<agent-identity-client-id>
```

The JWT assertion is signed by a private key stored in the OS credential store:
- **macOS:** Keychain (Secure Enclave on Apple Silicon)
- **Windows:** Certificate Store (TPM 2.0 on supported hardware)
- **Linux:** Secret Service API / GNOME Keyring

The private key never leaves the secure hardware. Only the public certificate is registered in Entra.

## Implementation Plan

1. Generate a self-signed X.509 certificate per device during `setup.sh`
2. Upload the public cert to the Blueprint app registration (automate via Graph API)
3. Store the private key in the OS credential store (extend `platform/` layer)
4. Modify `acquire_agent_user_token()` Hop 1 to construct a JWT assertion instead of sending `client_secret`
5. Remove `ENTRACLAW_BLUEPRINT_SECRET` from `.env` — it's no longer needed

## Alternatives Considered

| Alternative | Why Not |
|-------------|---------|
| Human token bootstrapping | Architecturally impossible — agent entities are confidential clients only |
| Workload Identity Federation | Requires external OIDC IdP with public endpoint — impractical for dev laptops |
| Azure Arc managed identity | Overkill — treating every laptop as a managed server |
| SPIFFE/SPIRE on device | Too complex — running a local OIDC issuer on a dev machine |

## Consequences

**Positive:**
- No plaintext secrets on disk
- Private key bound to device hardware (Keychain/TPM)
- Aligns with Microsoft's production recommendation
- Certificate rotation is device-local (no Entra portal visit needed if automated)
- The `platform/` layer already has OS-specific shims — natural extension

**Negative:**
- Certificate generation adds setup complexity (mitigated by automation in setup.sh)
- Per-device certificate provisioning (each device needs enrollment)
- Self-signed certs have no revocation infrastructure (acceptable for PoC)

**Risks:**
- Agent ID APIs are still in preview — certificate auth may have undocumented behaviors
- Initial certificate enrollment still requires admin action or a provisioning flow

## Related

- ADR-001: OBO Flows for Device Agents
- ADR-002: Agent User Over OBO
- Learning #3: Token responses return error dicts, not exceptions
- Learning #12: Three-hop flow requires fmi_path parameter
