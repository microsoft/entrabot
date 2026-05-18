# Agent prompt: migrate Provisioner from secret-auth to cert-auth

Paste the content below into Claude Code while working in the
`entraclaw-identity-research` repo directory. It is written to be
self-contained — the agent does not need any prior conversation context.

---

```
You are fixing a HIGH-severity security issue in this repo. Read
`docs/SECURITY-DEBT-PROVISIONER-SECRET.md` end to end before you do
anything else. It describes the problem, the fix, the migration
requirement, and the verification checklist. Do not deviate from it.

Reference implementation that has already shipped this fix:
`/path/to/persona-sati/scripts/entra_provisioning.py`
and `/path/to/persona-sati/scripts/provisioner-token.py`.

Read those two files. The shape is intentionally terse. Copy the same
pattern into `entraclaw-identity-research/scripts/entra_provisioning.py`
and a new `scripts/provisioner-token.py`. Change:

- `PROVISIONER_APP_DISPLAY_NAME` stays `EntraClaw Agent ID Provisioner` —
  do NOT rename. Cert-auth is a credential change, not an identity
  change.
- `_KEYCHAIN_SERVICE_CERT = "entraclaw-provisioner-cert"` (not
  persona-sati-provisioner-cert).
- `_STATE_FILE = .entraclaw-state.json` (already the case here).

Your job in order:

1. Update `pyproject.toml` (or equivalent) to add `keyring>=24` and
   `cryptography>=42` to the provisioning deps. `azure-identity` and
   `requests` are already there.

2. Rewrite `scripts/entra_provisioning.py`:
   - Add cert-auth helpers (_generate_provisioner_cert,
     _upload_cert_to_app, _remove_legacy_password_credentials,
     keychain get/store/delete).
   - Replace ClientSecretCredential with CertificateCredential in
     get_graph_token().
   - In ensure_app_registration(), migrate any pre-existing
     PROVISIONER_CLIENT_SECRET from the state file by:
       (a) reading and using it only to mint ONE last token if needed
           to clean up (optional — you can skip if the next step
           works without it)
       (b) deleting ALL passwordCredentials from the Provisioner app
           via az ad app credential delete
       (c) clearing PROVISIONER_CLIENT_SECRET from the state file
   - Generate a new cert, register it, store the key in Keychain.
   - Print a clear "migrated X password credentials; secret purged from
     disk" summary.

3. Create `scripts/provisioner-token.py` — a small CLI that calls
   get_graph_token() and prints the token. Exit 0 on success, 1 with
   error on stderr.

4. Audit every other place in this repo that acquires a Graph token.
   Grep for `ClientSecretCredential`, direct curl to
   `/oauth2/v2.0/token`, or reads of `PROVISIONER_CLIENT_SECRET`.
   Update each to use `get_graph_token()` from entra_provisioning, or
   shell out to `scripts/provisioner-token.py`. Zero secret usage
   anywhere.

5. If `setup.sh` builds JWT assertions or uses curl with client_secret
   for the Provisioner, replace that section with a call to
   `scripts/provisioner-token.py`.

6. Run the verification checklist from the security debt doc. If
   anything fails, fix it — do not open the PR.

7. Write a runbook at `docs/runbooks/cert-auth-migration.md` that a
   user can follow on their own machine. Include the pre-flight
   backup, the migration command, verification commands, and
   rollback.

8. Commit on a branch `fix/provisioner-cert-auth`, open a PR to main.
   In the PR body, explicitly cross-reference
   `docs/SECURITY-DEBT-PROVISIONER-SECRET.md` and link to the
   persona-sati reference implementation. State clearly that this
   closes HIGH-severity security debt.

DO NOT:
- Skip the migration of existing state (the password MUST be removed
  from both disk AND the app registration; leaving either is a
  backdoor).
- Cache the secret in environment variables or temp files as a
  "performance optimization."
- Commit `.entraclaw-state.json` or any keychain export to git.
- Rename the Provisioner app, change its permission set, or touch the
  Blueprint cert path.
- Open the PR without running the full verification checklist.

When the PR is open, summarize: which files changed, how many
password credentials were removed from the app in your testing, and
the commands you ran to verify no secrets remain on disk.
```

---

## One-time setup before giving the agent the prompt

Make sure your az CLI is logged in to the same tenant where the
Provisioner lives (otherwise the migration step that deletes password
credentials will fail):

```bash
az login
az account show --query "{name:name,tenantId:tenantId,user:user.name}" -o json
```

And back up the current state file, in case you need to roll back:

```bash
cp .entraclaw-state.json /tmp/entraclaw-state.backup-$(date +%s).json
```

Then paste the agent prompt and let it work.
