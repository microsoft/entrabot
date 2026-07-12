# Auth

Token acquisition modules. Source lives in `src/entrabot/auth/` and `src/entrabot/tools/teams.py` (the three-hop token functions sit alongside the Teams helpers because they share the same `httpx` client and token-refresh path).

See [Token Flows](../token-flows.md) for the wire-level flow diagrams and
[Identity and Token Flow](../../architecture/identity-and-token-flow.md) for
why certificate-based assertions replace client secrets.

## Certificate JWT assertion

### `build_client_assertion`

```python
def build_client_assertion(
    *,
    private_key_pem: str | None = None,
    cert_thumbprint: str,
    client_id: str,
    token_endpoint: str,
    cert_sha1: str | None = None,
) -> str
```

Build a signed JWT assertion for cert-based `client_credentials`. The assertion replaces `client_secret` in the OAuth2 token request — Entra validates the signature using the public certificate registered on the app.

- Mac / Linux: pass `private_key_pem`. Signing uses `cryptography` + `PyJWT`.
- Windows: omit `private_key_pem`, pass `cert_sha1` (40-char hex SHA-1 thumbprint of the cert in `Cert:\CurrentUser\My`). Signing happens via CNG against the non-exportable key — see `cncrypt_signer.sign_pkcs1_sha256`.

`cert_thumbprint` is the SHA-256 b64url thumbprint (`x5t#S256` per RFC 7515 §4.1.8).

### `compute_cert_thumbprint`

```python
def compute_cert_thumbprint(cert_pem: str) -> str
```

Compute the b64url SHA-256 thumbprint of a certificate. Used during cert generation and rotation.

### `sign_pkcs1_sha256` (Windows CNG)

`src/entrabot/auth/cncrypt_signer.py`:

```python
def sign_pkcs1_sha256(*, thumbprint: str, hash_bytes: bytes) -> bytes
```

Signs a 32-byte SHA-256 digest via `ncrypt.dll` PKCS1+SHA256 against the non-exportable cert key in `Cert:\CurrentUser\My`. Raises `ValueError` if `thumbprint` is not 40 hex characters or `hash_bytes` is not a 32-byte digest, `CertNotFoundError` if the thumbprint is not in the store, and `SigningError` on any other CNG/crypt32 failure.

## MSAL delegated auth

`src/entrabot/auth/delegated.py`:

### `MsalDelegatedAuth`

```python
class MsalDelegatedAuth:
    def __init__(
        self,
        client_id: str,
        tenant_id: str = "common",
        scopes: list[str] | None = None,
        port: int = LOCALHOST_PORT,
    ) -> None

    def try_silent(self) -> dict[str, Any] | None
    def authenticate(self) -> dict[str, Any]

    @property
    def accounts(self) -> list[dict[str, Any]]
```

`authenticate()` tries `try_silent()` first, then falls back to the localhost-redirect interactive flow (port 8400), then to device code, stopping at the first stage that succeeds:

- Falls through to localhost redirect when there is no cached/refreshable token.
- Falls through to device code when the port is in use, no browser can be opened, or the user does not complete within `LOCALHOST_TIMEOUT`.

`try_silent()` returns a cached token without UI when one is available. The MCP server calls `authenticate()` at startup (which tries silent acquisition internally) and calls `try_silent()` directly during token refresh, so a near-expiry token can be renewed without re-running the full interactive flow. The cache is persisted via `msal-extensions`' OS-encrypted `PersistedTokenCache`, falling back to an in-memory `msal.SerializableTokenCache` if persistent cache setup fails.

Used by `delegated` mode. Messages prefixed with `[EntraBot]` so humans can spot what the agent posted under the human's identity.

## Three-hop token chain

`src/entrabot/tools/teams.py` exposes the three functions that drive the Agent User identity model.

### `acquire_agent_user_token`

```python
def acquire_agent_user_token(
    config: EntraBotConfig,
    *,
    resource_scope: str = GRAPH_RESOURCE_SCOPE,
) -> str
```

Acquire a delegated token for the Agent User via the three-hop flow:

- **Hop 1** — Blueprint → `client_credentials` → Blueprint token.
- **Hop 2** — Agent Identity → FIC exchange (Blueprint token as assertion) → Agent Identity token.
- **Hop 3** — Agent User → `user_fic` grant → delegated user token (`idtyp=user`).

`resource_scope` selects the resource at Hop 3 only. Defaults to Graph (`https://graph.microsoft.com/.default`). Hops 1+2 always exchange against `api://AzureADTokenExchange/.default` (the FIC exchange scope).

Raises `AgentIDNotAvailable` if config is incomplete, `TokenExchangeError` if any hop fails.

### `acquire_agent_user_storage_token`

```python
def acquire_agent_user_storage_token(config: EntraBotConfig) -> str
```

Three-hop variant for Azure Blob Storage. Same first two hops; Hop 3 swaps the resource scope to `https://storage.azure.com/.default`. Requires the Agent Identity to be consented for Storage during `setup.sh --use-cloud-memory`.

### `acquire_agent_identity_token`

```python
def acquire_agent_identity_token(
    config: EntraBotConfig,
    *,
    resource_scope: str = GRAPH_RESOURCE_SCOPE,
) -> str
```

Two-hop variant. Stops at the Agent Identity — no `user_fic` grant. Used by `entrabot.identity.sponsors` to read the Agent Identity's Graph sponsors relationship, which requires app-only auth rather than the Agent User's delegated token.

## Common errors

Every token response is checked for an `"error"` key BEFORE accessing `"access_token"` — Entra returns error dicts, not HTTP exceptions, on most failures.

- `AgentIDNotAvailable` — config missing required fields (`blueprint_app_id`, `blueprint_cert_thumbprint`, `tenant_id`, `agent_id`, `agent_user_id`).
- `TokenExchangeError` — a hop failed. Carries `hop`, `error`, `description`.
- `TokenExpiredError` — a downstream Graph or Storage call returned 401; refresh the token.

## Related

- [Token Flows](../token-flows.md) — wire-level flow diagrams.
- [Identity](identity.md) — sponsor gating and the identity state machine.
- [Identity and Token Flow](../../architecture/identity-and-token-flow.md) — Entrabot's implementation and certificate-auth rationale.
- [Delegated Authentication](../../platform-docs/delegated-auth.md) — MSAL delegated auth specifics.
- [Agent Users](../../platform-docs/entra-agent-users.md) — the three-hop user-FIC flow.
- [Configuration](../configuration.md) — the full environment variable reference.
