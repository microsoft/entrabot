# SECURITY DEBT: Provisioner client_secret is persisted to local disk

**Severity: HIGH — confidentiality of agent identity infrastructure**
**Status: Open**
**Owner: the user**
**Filed: 2026-04-19**

---

## The problem, stated plainly

`scripts/entra_provisioning.py::ensure_app_registration()` creates a long-lived **client secret** on the `EntraClaw Agent ID Provisioner` app registration and writes it verbatim to **`.entraclaw-state.json`** at the repo root:

```python
set_state("PROVISIONER_CLIENT_SECRET", client_secret)
print("  Stored provisioner app secret in state file")
```

The file is gitignored. That is not sufficient. A secret on a local filesystem is:

- **Readable by anything running as that user** — any Node process, any npm install postinstall, any stray docker build that mounts the repo root, any backup utility that ignores `.gitignore`, any cloud-sync tool (iCloud Drive, Dropbox) that auto-includes the project folder.
- **Exfiltrated by the first compromise** — a single malicious Python package or VS Code extension gets `.entraclaw-state.json` and from there can mint Graph tokens with **Application.ReadWrite.All, DelegatedPermissionGrant.ReadWrite.All, User.ReadWrite.All, AgentIdentity.\*, AgentIdUser.\***. That combination can create agent users, impersonate them, grant arbitrary consent, and read the entire directory.
- **Impossible to rotate cleanly** without an out-of-band process. The file is the source of truth; if it's copied, there's no way to revoke the copy.

You already know this is wrong. The Blueprint private key lives in macOS Keychain (via `keyring`) exactly because a plaintext secret on disk is an unacceptable trust surface. The Provisioner got skipped when that discipline was applied to the Blueprint. It needs the same fix.

## Non-negotiable rule this violates

From the top of `CLAUDE.md`:

> **Private keys never leave the compute boundary they were minted for**
> **Never write secrets to logs or memory files**

Client secrets are secrets. `.entraclaw-state.json` is a file. The rule is clear.

## What correct looks like

The Blueprint solved this. Do the same thing for the Provisioner:

1. **Authenticate the Provisioner with a self-signed X.509 cert**, not a client_secret. Use `azure.identity.CertificateCredential` instead of `ClientSecretCredential`.
2. **Generate the cert on first bootstrap** using `cryptography` (RSA 2048, CN=`entraclaw-provisioner`, 365-day validity). Register the public cert on the app via `az ad app credential reset --cert @<tempfile> --append`. Discard the tempfile immediately.
3. **Store the private key in macOS Keychain** via `keyring` — exactly like `setup.sh` already does for the Blueprint cert. Service name convention: `entraclaw-provisioner-cert`, account = tenant id.
4. **Purge `PROVISIONER_CLIENT_SECRET` from `.entraclaw-state.json`** at the same moment. Never write it again.
5. **Delete any password credentials on the Provisioner app** via Graph (`az ad app credential delete --id <app-id> --key-id <id>`). A lingering password credential is a backdoor even after cert-auth is in place.
6. **deploy.sh (and any other shell caller)** shells out to a small Python helper (`scripts/provisioner-token.py`) which calls `get_graph_token()` from `entra_provisioning.py`. The helper prints the token on stdout. No bash construction of JWT assertions; the Python path uses `CertificateCredential` which reads the PEM from Keychain in memory only.

## Reference implementation

The persona-sati repo now has the cert-auth version. Read it to see the shape:

- `persona-sati/scripts/entra_provisioning.py` — `_generate_provisioner_cert`, `_upload_cert_to_app`, `_keychain_{get,store,delete}_cert`, `_remove_legacy_password_credentials`, reworked `ensure_app_registration()` and `get_graph_token()`.
- `persona-sati/scripts/provisioner-token.py` — the shell-callable Python helper.
- `persona-sati/docs/runbooks/identity-migration.md` — the user-facing migration runbook.
- `persona-sati/pyproject.toml` — the `[provisioning]` optional-dep group adds `keyring>=24` + `cryptography>=42`.

The file layout, naming, and migration path are all transferable to entraclaw with the service name changed (`entraclaw-provisioner-cert` vs `persona-sati-provisioner-cert`) and the state-file name changed (`.entraclaw-state.json` vs `.persona-sati-state.json`).

## Migration requirement (non-negotiable)

This is not a green-field change. Existing developer machines almost certainly have a live `PROVISIONER_CLIENT_SECRET` in `.entraclaw-state.json`. The fix MUST include a one-shot migration:

1. Read the existing `PROVISIONER_CLIENT_SECRET` from `.entraclaw-state.json` (needed only to verify the app is live — do not use it for anything else).
2. Generate the new cert and register it.
3. Store the private key in Keychain.
4. **Delete the password credential(s) from the app** via `az ad app credential delete --id <prov-app> --key-id <keyId>`. Enumerate all `passwordCredentials[].keyId` and delete each one. Zero password credentials must remain.
5. **Remove `PROVISIONER_CLIENT_SECRET`** from `.entraclaw-state.json`.
6. Print a prominent summary: "Migrated Provisioner from secret-auth to cert-auth. X password credentials removed from the app registration. PROVISIONER_CLIENT_SECRET purged from state file."

If any of those steps fails, abort the migration with a red error; never leave the app in a half-migrated state with both a cert AND a password credential.

## What is NOT being asked for

- **Do not rename the app registration.** Keep `EntraClaw Agent ID Provisioner`; adding a cert is a credential-type change, not an identity change.
- **Do not touch the Blueprint cert.** That path already works; leave it alone.
- **Do not change the three-hop flow on the agent side.** This fix is only about how the Provisioner authenticates to Graph during provisioning calls.
- **Do not add a web of abstractions.** One cert helper module, one token-mint helper, done. Read the persona-sati version — it is intentionally terse.

## Verification checklist (for the agent doing the work)

Before opening the PR:

- [ ] `grep -r PROVISIONER_CLIENT_SECRET scripts/ src/ docs/` returns nothing except comments in the migration code and this debt document.
- [ ] `grep -r ClientSecretCredential scripts/ src/` returns nothing (replaced with `CertificateCredential`).
- [ ] `az ad app show --id <your-provisioner-app-id> --query "passwordCredentials"` returns `[]` after running the migration on your own state.
- [ ] `security find-generic-password -a <tenant-id> -s entraclaw-provisioner-cert` finds the key.
- [ ] `.entraclaw-state.json` contains no `PROVISIONER_CLIENT_SECRET` field after the migration.
- [ ] `setup.sh` runs cleanly end-to-end on a machine that has run the migration.
- [ ] Every other script that acquires a Graph token (catch_up.py, dm.py, claude_memory_sync.py, diagnose-chat.py — grep for `ClientSecretCredential` or direct curl to `/oauth2/v2.0/token`) either uses `entra_provisioning.get_graph_token()` or a `CertificateCredential`. No direct secret usage anywhere.
- [ ] The token-mint helper is reachable from both Python and shell (deploy.sh, if any). Helper name and path documented in `README.md` or an equivalent spot.
- [ ] Test suite runs green. If the existing test suite mocked `ClientSecretCredential`, the mocks need to flip to `CertificateCredential`.
- [ ] A new runbook section (or a new file at `docs/runbooks/cert-auth-migration.md`) walks a user through running the one-shot migration, including backup steps and rollback.

## Why this is filed as a stern document, not a casual TODO

Because every week that passes with this unfixed is another week of:

- A live client_secret sitting in a known file path on your development machines.
- Any new developer who pulls this repo and runs setup.sh inheriting that same live secret.
- A broken symmetry with the Blueprint path — the latter is correct, the former is not, and the inconsistency itself is a smell that invites "why is the Blueprint cert in Keychain but the Provisioner secret on disk?" confusion in future reviews.

Treat this at the same severity as the Blueprint-cert work that was already done. It is not a lower-severity issue; it was just missed.
