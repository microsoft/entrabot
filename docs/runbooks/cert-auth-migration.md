# Runbook: Migrate the EntraBot Provisioner from secret-auth to cert-auth

**Status:** Active (one-time migration per developer machine)
**Risk:** Low — reversible within ~15 minutes. Affects only the Provisioner app's credential type.
**Duration:** ~2 minutes end-to-end if nothing goes wrong.

---

## Why this exists

`scripts/entra_provisioning.py` used to create a long-lived client secret on the `EntraBot Agent ID Provisioner` app and write it to `.entrabot-state.json` on disk. That violates the CLAUDE.md non-negotiable:

> **Never write secrets to logs or memory files**

It also broke the symmetry with the Blueprint cert, whose private key already lives in macOS Keychain. This runbook walks you through the one-shot migration that:

1. Replaces the Provisioner's client_secret with a self-signed X.509 cert (private key in Keychain).
2. Deletes all password credentials on the Provisioner app.
3. Purges `PROVISIONER_CLIENT_SECRET` from `.entrabot-state.json`.

See `docs/SECURITY-DEBT-PROVISIONER-SECRET.md` for the full rationale.

---

## What gets changed

| Component | Before | After |
|---|---|---|
| Provisioner credential | client_secret (365 days) on app | RSA 2048 self-signed cert (365 days) on app |
| Private key storage | `.entrabot-state.json` (plaintext on disk) | macOS Keychain (service `entrabot-provisioner-cert`) |
| Library path | `azure.identity.ClientSecretCredential` | `azure.identity.CertificateCredential` |
| State file fields | `PROVISIONER_CLIENT_SECRET` | `PROVISIONER_CERT_THUMBPRINT` |
| Shell token helper | n/a (inline curl with secret) | `scripts/provisioner-token.py` |

The Provisioner app **identity** (display name, object id, app id, permission set) is unchanged. This is a credential-type migration, not a rename or re-permission.

---

## Pre-flight checklist

- [ ] `az login` as the user that already owns the Provisioner app. Verify with:
  ```bash
  az account show --query "{name:name,tenantId:tenantId,user:user.name}" -o json
  ```
- [ ] Python venv is active and has the provisioning deps:
  ```bash
  source .venv/bin/activate
  pip install -e ".[dev,provisioning]"
  ```
- [ ] **Back up the current state file** so rollback is cheap:
  ```bash
  cp .entrabot-state.json /tmp/entrabot-state.backup-$(date +%s).json
  ```
- [ ] Note your Provisioner app ID (you'll need it for verification):
  ```bash
  jq -r .PROVISIONER_CLIENT_ID .entrabot-state.json
  ```

---

## Migration command

One shot. Idempotent — safe to re-run.

```bash
python3 scripts/entra_provisioning.py
```

What this does, in order:

1. Reads `PROVISIONER_CLIENT_SECRET` from `.entrabot-state.json` (if present) and immediately clears it from the state file.
2. Finds the existing `EntraBot Agent ID Provisioner` app (or creates it if missing).
3. Ensures the 25 Graph application permissions are registered and admin-consent is granted.
4. **Deletes all password credentials on the app** via `az ad app credential delete`. Prints a count.
5. Generates a fresh RSA 2048 cert (CN=`entrabot-provisioner`, 365 days).
6. Uploads the public cert to the app via `az ad app credential reset --cert @... --append`.
7. Stores the cert+key PEM bundle in macOS Keychain (service `entrabot-provisioner-cert`, account = tenant id).
8. Records the SHA-1 thumbprint in `.entrabot-state.json` as `PROVISIONER_CERT_THUMBPRINT`.

Expected runtime: ~45 seconds. Most of it is the 30-second Graph permission propagation wait.

On success you'll see (trimmed):

```
WARNING: legacy PROVISIONER_CLIENT_SECRET found in state file. Cert-auth supersedes secret-auth; purging the secret from disk.
Ensuring 25 Graph application permissions on provisioner app...
Provisioner app already has the required Graph permissions
Admin consent granted
Removed N legacy password credential(s) from Provisioner app (cert-auth only from here on).
Generating cert for Provisioner (RSA 2048, 365 days)...
Uploading public cert to app (SHA-1 thumb: ...)...
Cert private key stored in macOS Keychain (service='entrabot-provisioner-cert', account=<tenant>).
```

---

## Verification

Run each of these from the repo root. All four must pass.

```bash
TENANT=$(jq -r .TENANT_ID .entrabot-state.json)
PROV=$(jq -r .PROVISIONER_CLIENT_ID .entrabot-state.json)

# 1. Secret purged from state file
grep PROVISIONER_CLIENT_SECRET .entrabot-state.json && echo "FAIL" || echo "PASS: not found"

# 2. Zero password credentials on the app
az ad app show --id "$PROV" --query "passwordCredentials" -o json
# Expected: []

# 3. Keychain entry present (metadata only — do NOT pass -w unless you really need the PEM bundle)
security find-generic-password -a "$TENANT" -s "entrabot-provisioner-cert" -g 2>&1 | head -3
# Expected: entry with service "entrabot-provisioner-cert" and account <tenant>

# 4. Token helper emits a clean JWT for graph.microsoft.com
TOKEN=$(python3 scripts/provisioner-token.py)
echo "$TOKEN" | python3 -c "
import sys, json, base64
tok = sys.stdin.read().strip()
parts = tok.split('.')
assert len(parts) == 3, f'not a JWT ({len(parts)} parts)'
payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
print(f'PASS: aud={payload[\"aud\"]}, appid={payload[\"appid\"]}')
"
```

If any of the four fails, STOP and investigate. Do not ship a half-migrated state — the most dangerous state is one where the Provisioner has both a cert AND a lingering password credential (backdoor).

---

## Rollback (emergency only — 15-minute window max)

Rollback leaves you **exposed** — it re-creates the client_secret on disk that the fix was meant to eliminate. Use it only if the migration broke something you need running right now, and only long enough to diagnose. Then re-run the migration.

```bash
# 1. Restore the backup state file
cp /tmp/entrabot-state.backup-<timestamp>.json .entrabot-state.json

# 2. Re-create a client_secret on the Provisioner app so old code paths keep working
PROV=$(jq -r .PROVISIONER_CLIENT_ID .entrabot-state.json)
SECRET_JSON=$(az ad app credential reset --id "$PROV" --append --years 1 -o json)
SECRET=$(echo "$SECRET_JSON" | jq -r .password)

# 3. Re-write the secret into the state file (what the old code expected)
python3 -c "
import json, pathlib
sf = pathlib.Path('.entrabot-state.json')
data = json.loads(sf.read_text())
data['PROVISIONER_CLIENT_SECRET'] = '$SECRET'
sf.write_text(json.dumps(data, indent=2) + '\n')
"

# 4. Temporarily revert the code change (replay the commit locally)
git revert --no-commit HEAD  # adjust as needed to get back to the secret-auth version

# 5. (Optional) Purge the cert from Keychain
TENANT=$(jq -r .TENANT_ID .entrabot-state.json)
security delete-generic-password -a "$TENANT" -s "entrabot-provisioner-cert"
```

**After rollback, the secret is back on disk.** Don't leave it there. Diagnose the issue, re-run the migration, verify all four checks, then force-delete the rolled-back secret:

```bash
az ad app credential delete --id "$PROV" --key-id <keyId-of-new-secret>
```

---

## Post-migration actions

- [ ] `git status` shows no modified `.entrabot-state.json`, no Keychain export, no token output committed.
- [ ] Delete the backup state file from `/tmp/` once you're confident the migration held (it contains the now-invalidated secret, but it's still worth cleaning up):
  ```bash
  shred -u /tmp/entrabot-state.backup-*.json  # Linux
  # OR
  rm -P /tmp/entrabot-state.backup-*.json     # macOS
  ```
- [ ] Schedule a cert-rotation reminder for ~11 months from now (certs expire at 365 days). Rotation = re-run `python3 scripts/entra_provisioning.py` — it detects expired certs and re-mints.

---

## Known gotchas

- **"keyring is required for cert-auth"** — you forgot to install the provisioning deps. Run `pip install -e ".[dev,provisioning]"`.
- **"failed to upload cert to Provisioner app"** — your `az login` isn't the Provisioner app owner, or the account lacks Application.ReadWrite.All consent. Have an admin re-run the migration from their machine, or grant you the role.
- **Migration succeeds but `provisioner-token.py` 401s** — Graph permission propagation can lag. Wait 60 seconds and retry. `get_graph_token` already sleeps 30s by default; pass `wait_for_propagation=True` if re-running programmatically.
- **`passwordCredentials` is not `[]` after migration** — a concurrent process (CI job, another dev machine) re-created a secret. Re-run the migration; the `_remove_legacy_password_credentials` step is idempotent.
