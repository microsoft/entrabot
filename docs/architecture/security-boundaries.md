# Security Boundaries

This page collects the boundaries Entrabot enforces between the agent, the human, external content, and the tools it exposes.

## Identity boundary: Agent User vs. delegated human

Every action the agent takes against Graph is attributed to one of two principals, and the attribution is structural, not cosmetic:

- **`agent_user` mode** — the three-hop Blueprint → Agent Identity → Agent User flow (`tools/teams.py::acquire_agent_user_token`) mints a token with `idtyp=user` carrying the Agent User's own object ID. Graph sees the agent as a first-class user principal, distinct from both the human and an app-only service principal.
- **`delegated` mode** — `MsalDelegatedAuth` uses the signed-in human's own token. There is no Agent User attribution in this mode; outbound messages are prefixed `[EntraBot]` so the human can tell which messages the agent sent, and the identity state machine records `attribution_type = "delegated-human"`.

`tools/audit.py::log_event()` is what makes this durable: every audit event carries an `attribution_type` of `"agent"`, `"delegated-human"`, or `"none"`, resolved from the active identity session, then config, then the credential store, in that order. See [Identity and Token Flow](identity-and-token-flow.md) for the full three-hop mechanics.

## Certificate boundary (per OS)

Certificate handling differs by platform, and the two paths are not equivalent in strength:

- **macOS and Linux** — `_build_blueprint_assertion()` retrieves the PEM private key from the OS keystore (Keychain via `MacCredentialStore`, Secret Service/KWallet via `LinuxCredentialStore`) **into the process**, then signs the JWT in-process with `cryptography`'s `load_pem_private_key` and PyJWT. The key material is present in process memory for the duration of the signing call.
- **Windows** — the private key is a non-exportable CNG key in `Cert:\CurrentUser\My`, backed by the Microsoft Platform Crypto Provider (TPM) when available or the Microsoft Software Key Storage Provider otherwise (`generate_windows_cert.py`, `auth/cncrypt_signer.py`). Signing happens through `ncrypt.dll`; only the signature crosses back into the process, never the key.

Both paths produce the same JWT shape (`x5t#S256` header, 10-minute assertion lifetime — `auth/certificate.py`), so the two are interchangeable at the protocol level. They are **not** interchangeable at the threat-model level: only the Windows CNG path is non-exportable, and only a TPM-backed CNG key is hardware-backed. Don't describe the Mac/Linux path as hardware-backed or non-exportable — the PEM is retrievable by anything that can read the OS keystore under the agent's account.

## Audit-first, fail-closed attribution

`log_event()` is called before a sensitive action proceeds, and again after it resolves, for security-sensitive call sites (Files Graph calls via `_audit_graph_call()`, Teams member adds, file shares): a `pending` event first, then `success` or `failure`. Other tools audit once, immediately before the side effect.

Attribution itself fails closed: if `agent_id` isn't supplied and none can be resolved from the active identity session, config, or the credential store, `log_event()` raises `AuditAttributionError` rather than logging `"unknown"` — **unless** the caller explicitly opts in with `attribution_type="none"` (reserved for bootstrap/preflight code that genuinely runs before any identity exists). This scope is specifically about *attribution*; it does not mean every possible agent action is audited today — it means an action that is meant to be agent-attributed cannot silently become an orphaned log line.

## External-content / XPIA boundary

`security/xpia.py::wrap_external()` wraps external-source text — Teams messages, email bodies, file contents, Work IQ content — in a `<external_content>` envelope before it reaches the model:

```
<external_content source="..." sender="..." received_at="...">
  ...body...
</external_content>
```

The properties that make this a real boundary, not just formatting:

- **The outer envelope is always authoritative.** `wrap_external()` unconditionally wraps every call, even when the body already looks like an envelope. External text cannot forge the trusted `source`/`sender`/`received_at` metadata by embedding what looks like the wrapper prefix — the caller-supplied attributes always win because they're the ones written into the outer tag.
- **Escape-on-collision.** Any literal `</external_content>` in the body — including case variants and whitespace-padded forms like `< / External_Content >` — is entity-escaped before wrapping, so embedded text can't break out of the envelope by closing it early.
- **Attribute-safe.** `source`, `sender`, and `received_at` values are escaped for `&`, `<`, `>`, and `"`, so a hostile value like `source="teams:<script>"` can't open a new tag inside the attribute region.
- **Round-trip support for tests and audit.** `unwrap_external()` reverses collision escaping and round-trips the body byte-for-byte while recovering the `source`, optional `sender`, and optional `received_at` attributes. The envelope has no `content_hash` attribute or cryptographic binding.
- **Opt-out is an env flag, not a code path.** `ENTRABOT_XPIA_WRAP_ENABLE=false` short-circuits `wrap_external()` to the identity function for rollback, without touching call sites.

This is a text-escaping and provenance boundary, not a cryptographic signature.

## Sponsor authorization

"Sponsor" is Entrabot's authorization concept for who may direct the agent to take mutating action on a human's behalf.

- **Source of sponsors.** `identity/sponsors.py::fetch_agent_identity_sponsors()` reads the Agent Identity's `/sponsors` Graph relationship using an app-only, two-hop token (`acquire_agent_identity_token()` — stops after the FIC exchange, no `user_fic` grant). This has to be app-only: the Agent User's own delegated token cannot read its own `/sponsors` relationship.
- **`SponsorGate`** (`identity/sponsors.py`) decides whether an inbound message can satisfy an explicit `wait_for_sponsor_dm` or server-side auto-wait. It matches on sponsor user ID, UPN, or email, extended with chat-member and cross-tenant B2B lookups across watched chats.
- **Active-channel binding** (`identity/active_channel.py::ActiveChannelBindings`) is the separate, TTL-scoped gate that authorizes *mutations*. A binding is minted only after a sponsor's inbound message has been **successfully pushed** to the model (via the channel-push notification path) — recording `(sponsor_user_id, chat_id, graph_sent_at)`. The TTL (120s) is enforced against the message's Graph-authored `sent_at`, not against when the server observed it, specifically so a stale message can't be replayed at bootstrap to mint a fresh authorization window. Bindings expire on read as well as on write, so a clock rewind can't resurrect one.
- **Mutating tools require both.** `add_teams_member()` (`tools/teams.py`) and `share_file()` (`tools/files.py`) both gate on: (1) the requester matching a sponsor identity, (2) an active-channel binding for that sponsor whose `chat_id` matches the one supplied, and (3) — defense in depth — the sponsor actually being a Graph member of the target chat. `SponsorChannelMismatchError` is raised specifically to catch a confused-deputy pattern: an LLM in chat A being induced to act on chat B, where the sponsor is a genuine member of B but not actively engaged there right now.
- **For `share_file`, only the requester is gated — not the recipient.** A sponsor may direct the agent to share a file with anyone; the recipient email is unrestricted. Do not describe `share_file` as gating both sides.
- **Wait validation and mutation authorization are separate.** `SponsorGate.accepts()` decides whether an inbound message can satisfy a sponsor wait. The general background poll delivers human messages without using `SponsorGate` as its filter; sponsor lookup is used after successful delivery only to decide whether to mint an active-channel binding. That binding, not `SponsorGate`, authorizes `add_teams_member` and `share_file` mutations.

## Server-side auto-wait, not a model-facing switch

`send_teams_message`'s decision to block for a sponsor reply is made by inspecting the connected MCP client's `clientInfo.name` server-side (`mcp_server.py::_current_host()`), against a fixed set of known channel-push hosts. There is no tool parameter the model can set to skip or force this behavior — if there were, an LLM would be free to set it however the current turn's text happens to suggest, which defeats the point of a behavioral guarantee. If a new host needs different auto-wait behavior, that's a server-side allowlist change, not something exposed at the tool boundary.

The `_COMMITMENT_PATTERNS` outbound-discipline check (also in `mcp_server.py`) is a **warning, not a block**: it looks for commitment-sounding language in outbound Teams text without a matching recent `add_promise` call, and appends a `_discipline_warning` to the result JSON when it fires. It does not stop the message from sending.

## Storage and cursor fail-closed behavior

Two storage-layer boundaries are part of the same fail-closed posture as the rest of this page:

- **Half-configured Blob storage is a hard error.** `storage/backend.py::get_backend()` raises `BackendMisconfiguredError` when exactly one of `ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER` is set, rather than silently falling back to an empty local store.
- **Ambiguous cursor reads never authorize a push.** `tools/chat_cursors.py::resolve_cursor()` classifies every cursor read as `ABSENT`, `PRESENT`, or `UNRESOLVED`; only `ABSENT` (a clean, successful "nothing here yet" read) allows a poll to treat a chat as new. A read failure, a timeout, or a corrupt payload is `UNRESOLVED` and is treated the same as "don't push," because an ambiguous read could be hiding a live cursor.

See [Storage and Memory](storage-and-memory.md) for the full mechanics of both.

## Efferent-copy trust boundary

The efferent-copy middleware (`src/entrabot/efferent_copy.py`) is disabled by default — it only wraps tool functions when `EFFERENT_COPY_ENABLE=1` is set, and `EFFERENT_COPY_DISABLE=1` always wins over the enable flag. When enabled, any MCP peer in `.mcp.json` that exposes a schema-compatible `observe(tool_name, args[, result])` tool is treated as **part of the trusted observability boundary** — discovery is schema-based, not name-based, so there's no allowlist of specific peer identities beyond `EFFERENT_COPY_SINKS` if an operator sets one.

Sensitive values are redacted before leaving the body by a case-insensitive, substring-based denylist (`token`, `secret`, `password`, `client_secret`, `access_token`, `api_key`, `bearer`, `credential`, `private_key`, and similar). This is **name/key-substring redaction, not semantic content inspection** — a sensitive value stored under a non-obvious key name (the module's own example: `auth_blob`) is not caught by the denylist and can reach a sink unredacted. Treat this as a real but bounded mitigation, not a guarantee that no sensitive data ever reaches an attached sink.

## Current limitation: no shipped execution sandbox

There is no `src/entrabot/sandbox/` package in this branch, and no process- or filesystem-level containment ships today for code the agent might execute or files it might touch beyond normal OS user permissions. Sandboxing/execution containment remains under evaluation rather than implemented — do not describe it as an available or enabled feature.

## See also

- [Audit Layer](layers/audit.md) — the event schema and attribution resolution in detail.
- [Identity and Token Flow](identity-and-token-flow.md) — the three-hop flow and per-OS certificate signing.
- [MCP Runtime](mcp-runtime.md) — server boot, token refresh, and where these boundaries are wired in.
- [Messaging and Delivery](messaging-and-delivery.md) — how inbound messages reach the sponsor gate and the active-channel binding.
- [Storage and Memory](storage-and-memory.md) — backend fail-closed behavior and cursor concurrency.
