# macOS Platform APIs

This page is the platform reference for how Entrabot uses macOS system services:
where it stores secrets, how it signs the Blueprint certificate assertion, how
delegated tokens are cached, and the setup surface that provisions everything.

## Credential storage: Keychain

Entrabot's `CredentialStore` on macOS is `MacCredentialStore`
(`platform/mac.py`), a thin wrapper over the `keyring` library that maps to the
login Keychain through `keyring.backends.macOS.Keyring`. It exposes the
OS-agnostic `store()` / `retrieve()` / `delete()` contract used across the
codebase for small string secrets such as the active client ID and the Blueprint
private key.

**Backend allowlist, fail closed.** At construction, `MacCredentialStore` calls
`assert_allowed_keyring_backend("Darwin")` (`platform/keyring_backend.py`). If
the active `keyring` backend is anything other than `keyring.backends.macOS.Keyring`
— or if the backend cannot be inspected at all — it raises
`InsecureKeyringBackendError` and refuses to proceed rather than silently writing
secrets to an unexpected store. Reaching the Keychain requires the user's login
Keychain to be accessible in the current session; a session where it is locked or
unavailable surfaces as a keyring failure rather than a silent fallback.

## Blueprint certificate signing

`platform/keycert.py` keeps the Blueprint certificate's lifecycle (create, find,
test-sign, delete) on top of the Keychain, with the same functions
`platform/wincert.py` has on Windows, so setup, certificate rotation, teardown,
the health checks and the runtime treat a certificate the same way on every
system.

- **One Keychain item per certificate.** Service `entrabot-blueprint-cert`,
  account the certificate's SHA-1 thumbprint (40 hexadecimal characters),
  holding the RSA-2048 private key and the certificate together, as PEM. An
  `index` item under the same service lists the thumbprints. `.env` names the
  agent's certificate in `ENTRABOT_BLUEPRINT_CERT_SHA1` (and
  `ENTRABOT_BLUEPRINT_KSP=keychain`), so two agents set up under one macOS
  account are two items, and neither overwrites the other's key.
- **A key is only used once it has signed.** Setup writes the item, reads it
  back, signs test data with the key and verifies the signature against the
  certificate before it registers anything. Deleting a certificate deletes its
  key: they are one item.
- **Fail closed.** A Keychain that cannot be read stops setup before anything
  is created, and stops the agent's sign-in. Nothing is ever written to a file
  instead.

When Entrabot signs the Hop 1 client assertion with `ENTRABOT_BLUEPRINT_CERT_SHA1`
set, it reads the key stored with that certificate into the process, checks that
the certificate is in date (logging a warning when 30 days or fewer remain), and
signs the JWT in-process with `cryptography` and PyJWT
(`auth/certificate.py::build_client_assertion`, passed `private_key_pem`). The
assertion header carries the base64url SHA-256 (`x5t#S256`) of the DER
certificate and has a 10-minute lifetime.

Setup's own Provisioner certificate is kept the same way, under service
`entrabot-provisioner-cert`, one item per tenant (account: the tenant ID).

**A Mac set up before the wizard.** Earlier versions of `setup.sh` stored the
Blueprint private key on its own, as a PEM string under service `entrabot` and
account `blueprint-private-key`, with no SHA-1 thumbprint in `.env`. Without
`ENTRABOT_BLUEPRINT_CERT_SHA1` the agent still signs with that key, so such a Mac
keeps working. The next `./scripts/setup.sh` keeps the key and, when its
certificate is on the agent's Blueprint, stores the two together as the agent's
own item (see
[Computers set up before the wizard](../reference/scripts/setup/setup-sh.md#computers-set-up-before-the-wizard)).
The older item is left in place; teardown deletes it only when it holds the same
key. While `.env` names a certificate, the agent never reads the older item: it
may be another agent's.

**Over SSH.** The login keychain is usually locked in an SSH session. Unlock it
there with `security unlock-keychain` before running setup or starting the
agent, or use the Mac's own desktop.

This path is **not** hardware-backed and the key is **not** non-exportable:
anything that can read the Keychain under the agent's account can retrieve the
PEM. That is the deliberate macOS trade-off, distinct from the Windows
non-exportable CNG path. See
[Security Boundaries](../architecture/security-boundaries.md) for the threat-model
comparison, and
[Windows and Platform Support](../architecture/windows-and-platforms.md) for the
per-OS certificate matrix.

Once the signed assertion exists, the rest of the token flow is identical to
every other platform — see
[Identity and Token Flow](../architecture/identity-and-token-flow.md).

## Delegated-mode token cache

In delegated mode, MSAL persists its token cache through msal-extensions'
`build_encrypted_persistence`, which on macOS encrypts the cache via the
Keychain (`auth/delegated.py`). The cache file lives under the per-user cache
directory resolved by `platformdirs` (`platformdirs.user_cache_dir("entrabot")`),
and its parent directory is created with `0o700` permissions. If encrypted
persistence cannot be built, Entrabot logs the failure and falls back to an
in-memory cache for that run rather than writing an unencrypted cache to disk.

## Setup surface

- **`scripts/setup.sh`** builds `.venv` and starts the setup wizard, the same one
  Windows runs: it signs in (installing a private Azure CLI under
  `~/.entrabot/tools/azure-cli` when there is none), provisions the Blueprint →
  Agent Identity → Agent User chain, creates or reuses the Blueprint certificate
  in the Keychain, writes `.env`, registers the Entrabot MCP server, and runs an
  end-to-end test. Operational storage stays local by default; pass
  `--use-cloud-memory` to opt into Azure Blob-backed storage.
  `./scripts/setup.sh --rotate-cert` replaces the certificate, and
  `./scripts/setup.sh --diagnose` checks the setup.
- **`start-entrabot.sh`** starts the agent in Claude Code.
- **`scripts/teardown.sh`** disconnects this Mac from its agent.
- **`scripts/prereqs-macos.sh`** is optional. It installs the Xcode Command
  Line Tools and, through Homebrew, Python 3.12+, git and the Azure CLI; with
  `--with-a365-cli`, also the .NET SDK, Microsoft's Agent 365 CLI and
  PowerShell 7, which setup does not need. It is safe to re-run and skips
  anything already present.

For step-by-step setup, see [macOS and Linux setup](../getting-started/macos-linux.md)
and [Prerequisites](../getting-started/prerequisites.md). Setup on macOS has
not yet been run end to end against a real tenant; see
[What has been tested](../getting-started/macos-linux.md#what-has-been-tested).

## Default paths

- **Operational data, logs, and audit** default to `~/.entrabot/` (for example
  `~/.entrabot/data`, `~/.entrabot/logs`, `~/.entrabot/audit`), each overridable
  by its corresponding `ENTRABOT_*_DIR` environment variable (`config.py`).
- **The MSAL delegated token cache** lives under the `platformdirs` per-user
  cache directory for `entrabot`, as described above.

## See also

- [Windows and Platform Support](../architecture/windows-and-platforms.md) — the
  cross-platform `CredentialStore` abstraction and per-OS certificate matrix.
- [Security Boundaries](../architecture/security-boundaries.md) — why the
  PEM-in-process model differs in strength from the Windows CNG path.
- [Identity and Token Flow](../architecture/identity-and-token-flow.md) — the
  three-hop flow the signed assertion feeds into.
- [Linux Platform APIs](platform-linux.md) and
  [Windows Platform APIs](platform-windows.md) — the other two platforms.
