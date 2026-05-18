# ADR-003: Certificate Auth Over Client Secrets for Device-Local Agents

**Date:** 2026-04-06
**Status:** Proposed
**Deciders:** the user
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

## 2026 amendment — per-platform reality

The original ADR was written against the Mac/Linux baseline where the
private key is stored as a PEM blob in ``keyring`` and JWT signing
runs through ``cryptography.load_pem_private_key``. The Windows port
(``feat/windows-port`` branch, see
``docs/architecture/PLAN-windows-port.md``) materially diverges — the
ADR's spirit holds, but the mechanics are now per-platform:

| Aspect | Mac / Linux | Windows |
|---|---|---|
| Key storage | Keychain / Secret Service via ``keyring`` (PEM blob) | ``Cert:\\CurrentUser\\My`` via Windows CNG |
| Key extractability | PEM is exportable; protected by OS access control | TPM KSP keys are **non-exportable**; software KSP is DPAPI-bound |
| Signing path | ``cryptography.load_pem_private_key`` → ``rsa.sign`` | ``ncrypt.dll`` ``NCryptSignHash`` (PKCS1+SHA256) via ``auth/cncrypt_signer.py`` |
| Cert generation | OpenSSL via ``scripts/generate_cert.py`` | ``New-SelfSignedCertificate`` via ``scripts/generate_windows_cert.py`` |
| KSP selection | n/a — software-only on Mac/Linux | TPM-first (``Microsoft Platform Crypto Provider``), software-fallback (``Microsoft Software Key Storage Provider``) |
| Thumbprint | SHA-256 b64url (used as JWT ``x5t#S256``) | SHA-1 hex (used to find cert in store) **plus** SHA-256 b64url (header) |
| Rotation | ``deploy.sh`` | ``deploy-windows.ps1`` + ``rotate_cert_windows.py`` (transactional rollback per D7) |

The dispatch lives in ``src/entraclaw/auth/certificate.py``:
``build_client_assertion`` accepts either ``private_key_pem`` (Mac/Linux)
or ``cert_sha1`` (Windows). Callers in ``tools/teams.py`` go through
``_build_blueprint_assertion`` which selects by ``sys.platform``.

The TPM-first/software-fallback decision is not a security weakness —
the software KSP still binds the private key to the user profile via
DPAPI, which matches the Mac/Linux baseline. The TPM path is strictly
stronger because the key cannot leave the chip.

The Windows port does NOT change the JWT shape, the audience, the
scopes, or the three-hop flow. From Entra's perspective, the same
``client_assertion_type`` arrives — only the local mechanics differ.

### Rotation invariants (D7, D13)

The rotation contract added with the Windows port hardens what the
original ADR left implicit on Mac/Linux too:

1. Capture the **old** public DER bytes BEFORE generating a new cert.
   For non-exportable TPM keys this is the only chance.
2. PATCH new DER → smoke test → on success delete old cert; on
   failure re-PATCH old DER + restore ``.env`` + invalidate MSAL cache.
3. If the rollback PATCH itself fails, halt loud
   (``ManualInterventionRequired``) — do not attempt heuristic
   recovery. Operator must triage by hand.

These invariants are exercised by ``tests/test_deploy_rollback.py``
on every host (cross-platform).

