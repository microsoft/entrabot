# Windows Port: Agent Identity Provisioning Parity

**SUPERSEDED 2026-04-28** by [`docs/architecture/PLAN-windows-port.md`](./architecture/PLAN-windows-port.md).
Kept for the Python-orchestrator-with-PS1-shim recommendation (which was adopted).

**Status:** Draft (architecture plan, no code) — written by Agent 2 / Product Manager review
**Date:** 2026-04-24
**Audience:** Brandon Werner, future Windows-machine contributor
**Source-of-truth scripts referenced:** `scripts/setup.sh`, `scripts/entra_provisioning.py`, `scripts/create_entra_agent_ids.py`, `scripts/provision_blob_storage.py`, `scripts/teardown.sh`, `src/entraclaw/platform/windows.py`, `src/entraclaw/auth/certificate.py`, `src/entraclaw/tools/teams.py`, ADR-003, ADR-005, hard-won-learnings.md (#1, #2, #5, #7, #8, #29, #34, #36)

---

## Executive summary

**Recommended path:** *Ship a thin PowerShell shim (`scripts/setup-windows.ps1`, `scripts/deploy-windows.ps1`, `scripts/teardown-windows.ps1`) that delegates 95% of the work to a new pure-Python orchestrator (`scripts/entraclaw_setup/__main__.py`). Use the existing `keyring` Windows-Credential-Locker backend for the Blueprint private key in v1. Defer Windows Certificate Store / CNG / TPM-backed keys to v2 behind a feature flag.*

Rationale in one paragraph: setup.sh is already mostly Python under a bash orchestrator. The Windows gap is **not** identity-protocol differences (Entra and Graph behave identically) and **not** the basic credential-store abstraction (`keyring` has a Windows backend today). The gaps are: (a) bash-isms in `setup.sh` (color escapes, `$(…)` capture patterns, `/tmp` writes, `chmod 600`, interactive `read -p`), (b) `az` CLI bootstrap UX on Windows (PowerShell execution policy, `az login` browser handling), and (c) the unmet aspirational claim in ADR-003 that Windows uses "Certificate Store (TPM 2.0)" — today it does not, on any platform. We can reach Mac parity by extracting the bash logic into Python and adding a thin PowerShell wrapper. TPM-backed CNG storage is a separate, larger v2 effort with real value but real schedule risk.

**Key recommendation:** Do *not* re-implement setup.sh as native PowerShell. Move the orchestration logic into Python (a `scripts/entraclaw_setup/` package), which already runs cross-platform, and ship two thin PS1 wrappers (one for setup, one for deploy/MCP wiring). Same approach for teardown. This keeps Mac parity automatic — every fix lands in one place.

---

## Status block

| Item | State |
|---|---|
| Mac/Linux setup | Production, idempotent, 654 tests green |
| Windows credential-store class | Exists (`platform/windows.py`), backed by `keyring`/Windows Credential Locker, **not exercised by coverage today** |
| Windows cert-auth path | Untested. Should work in theory because `cryptography`/`PyJWT` are pure Python; the cert generation + upload code in setup.sh would need to run from Python, not bash |
| Windows setup script | Does not exist |
| Windows deploy script | Does not exist |
| Windows MCP wiring (`.mcp.json`, `~/.copilot/mcp-config.json`) | `scripts/mcp_config.py` accepts an absolute binary path and writes it verbatim; Windows setup must pass `.venv\Scripts\entraclaw-mcp.exe` and add path-shape tests |
| TPM/CNG-backed Blueprint key | Aspirational only — ADR-003 mentions it but no implementation exists on any platform |

---

## Current macOS/Linux setup flow (as found in `scripts/setup.sh`)

The 8-step flow:

1. **Prereqs** — `az`, `python3.12+`, `git` on `PATH`. Python is auto-discovered (`python3.12`/`python3.13`/`python3`) and version-checked via `bc` or a Python fallback.
2. **Azure login verification** — `az account show`, captures `tenantId`, signed-in UPN, signed-in user object ID. Optional `--switch-user` re-runs `az login`. Optional `--teams-user=` resolves recipient(s) and detects B2B-guest UPNs (`#EXT#`) for federated chat.
3. **Provisioning prereqs** — `pip install azure-identity requests` into `.venv` if it exists, else system Python.
4. **Provisioner bootstrap** — runs `scripts/entra_provisioning.py`. This creates (or rediscovers) the dedicated **EntraClaw Agent ID Provisioner** app registration. The provisioner authenticates with a **certificate JWT** (private key in `keyring`, public cert on the app reg) and holds Graph application permissions for Blueprint/Agent Identity/Agent User CRUD + delegated permission grants. **Critical:** `az` CLI tokens are *never* used for Agent Identity APIs — Learning #1 (Directory.AccessAsUser.All causes 403).
5. **Identity creation** — runs `scripts/create_entra_agent_ids.py`. Creates Blueprint, **explicitly** creates `BlueprintPrincipal` (Learning #2 — not auto-created), creates Agent Identity (sponsor = signed-in user; Learning #5 — sponsors must be users, not SPs), creates Agent User, grants `oauth2PermissionGrant` for Graph and (if cloud memory) Storage scopes. Uses retry/backoff for permission propagation (Learning #8). Persists everything to `.entraclaw-state.json`.
6. **Blueprint cert** — creates `.venv` if missing, `pip install -e ".[dev]"`, generates self-signed RSA-2048 cert in Python, computes SHA-256/base64url thumbprint, stores PEM private key in `keyring` (`service="entraclaw"`, `key="blueprint-private-key"`), uploads public cert to Blueprint app via Graph `PATCH /applications/{id}` `keyCredentials`. Idempotent: cached thumbprint is verified against Entra; if missing, regenerates after warning the user.
7. **Venv + .env** — full editable install, writes `.env` (`chmod 600`) with all `ENTRACLAW_*` config. **No secrets** — only IDs, the cert thumbprint, and config flags.
   - **7b. Optional blob provisioning** (`--cloud-memory` flag) — `scripts/provision_blob_storage.py` ensures `entraclaw-rg` resource group, a tenant-scoped storage account, a per-Agent-User container, and `Storage Blob Data Contributor` RBAC scoped to the container. Storage scope also requires its own `oauth2PermissionGrant` (Learning #34) — handled in step 5.
8. **Summary + MCP wiring** — invokes `scripts/mcp_config.py` to write/upsert the entraclaw entry in both `.mcp.json` (project-local) and `~/.copilot/mcp-config.json`.

State files: `.entraclaw-state.json` (provisioning state, idempotency keys), `.env` (runtime config, mode 600).

---

## Windows capability analysis

### Prerequisites the user must install (not provisioned by us)

| Tool | Recommended source | Notes |
|---|---|---|
| **Python 3.12+** | python.org installer or `winget install Python.Python.3.12` | Must be on `PATH`. The python.org installer does this; the Microsoft Store build does NOT and breaks `pip install -e` because of write-restricted site-packages. **Detect and refuse Microsoft Store Python.** |
| **Azure CLI** | `winget install Microsoft.AzureCLI` (preferred) or MSI from aka.ms/installazurecliwindows | After install, requires a new shell so `PATH` picks up `az.cmd`. |
| **git** | `winget install Git.Git` | Needed for `pip install -e .` if the user clones; not strictly needed at runtime. |
| **PowerShell 7.4+** | `winget install Microsoft.PowerShell` (preferred over Windows PowerShell 5.1) | `setup-windows.ps1` should target 7.x and warn if running under 5.1. The `pwsh` shebang/host check is one line. |
| **VS C++ Build Tools** | Already present on most dev boxes | `cryptography` ships pre-built wheels for Windows on `pip>=21`, so this is usually a no-op — **document the fallback** if `pip install cryptography` ever needs to compile (rare for 3.12). |

### Verified-or-likely-OK on Windows

- **`keyring` → Windows Credential Locker.** The `keyring` package's default Windows backend is `keyring.backends.Windows.WinVaultKeyring`, which stores credentials in the per-user **Windows Credential Manager** (Generic Credentials, scoped to the logged-in user profile). A 2048-bit RSA PEM is small enough that this should work, but Windows credential-size documentation is inconsistent. **Verification step before shipping:** round-trip the actual generated PEM through `keyring.set_password`/`get_password` on a Windows VM and confirm. If size is an issue, fall back to splitting the PEM across two credential entries, or move to the Cert Store path earlier.
- **`cryptography` cert generation.** Pure-Python, identical behavior across OSes.
- **`PyJWT` JWT signing.** Pure-Python.
- **`httpx` token requests / Graph calls.** Pure-Python.
- **`mcp` SDK / FastMCP.** Pure-Python; the Claude Code / Copilot CLI clients work on Windows.
- **`az login` flow.** Defaults to interactive browser auth. Works on Windows 10/11 with a default browser. If running in a headless RDP session, falls back to device code.
- **PowerShell execution policy.** Default on Windows 11 is `RemoteSigned`. We do *not* sign our `.ps1` scripts. `setup-windows.ps1` will be blocked by default. Mitigations (in order of preference):
  1. Document `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` once-per-user.
  2. Provide a `setup-windows.cmd` shim that invokes `pwsh -ExecutionPolicy Bypass -File scripts\setup-windows.ps1`. Keeps the PS1 unsigned but unblocks the user with one double-click. **Recommended.**
  3. Sign the scripts with a dev cert. Out of scope for v1 (introduces a signing-cert distribution problem).

### Genuine Windows-only concerns

- **`chmod 600` on `.env`** — does not exist on Windows. Replace with NTFS ACL: `icacls .env /inheritance:r /grant:r "$env:USERNAME:R"` (read-only to the current user, no inheritance from the project directory's looser ACL). Exposing the file to other local users would matter only on shared workstations, but it's a non-zero risk and easy to fix.
- **Path separator + drive-letter handling in `mcp_config.py`** — must emit Windows paths. The existing tests need a Windows-path case.
- **`/tmp` writes in `setup.sh`** — `/tmp/entraclaw-provision-stdout.$$` (line 734). Doesn't exist on Windows. Already a known smell. The Python rewrite removes this entirely (use a tempfile or in-memory pipe).
- **Bash heredocs writing `.env`** — translate to Python `pathlib.Path(".env").write_text(...)`.
- **Color escape codes** — `\033[…]` works in modern Windows Terminal and PowerShell 7. Works in conhost on Windows 10 1909+ if `VirtualTerminalLevel` is enabled (default since 1809). Use `colorama` or `rich` from Python so we stop caring.
- **Interactive `read -r -p`** — replace with Python `input()` from the orchestrator. Make non-interactive defaults explicit.
- **Hostname tag for cert displayName** — `socket.gethostname()` works on Windows. Already used.

### Items unchanged from Mac/Linux

- Entra Graph beta API behavior, including all Agent Identity quirks (BlueprintPrincipal not auto-created, sponsors must be users, permission propagation 30–120s, `oauth2PermissionGrant` requires `startTime`, etc.). These are tenant-side.
- The three-hop token flow itself (`acquire_agent_user_token` in `tools/teams.py`) is pure Python and works as-is on Windows once the credential store returns the PEM.
- `provision_blob_storage.py` shells out to `az` — on Windows, `az` resolves to `az.cmd`. Python's `subprocess.run(["az", ...])` finds it on `PATH` and works. No code change.

---

## Gap analysis: current repo vs. Windows parity

| Gap | Severity | Where | Fix |
|---|---|---|---|
| `setup.sh` is bash-only | **Blocker** | `scripts/setup.sh` (903 lines) | Extract orchestration logic into a Python package (`scripts/entraclaw_setup/`). New PS1 wrapper invokes it. Mac/Linux can keep the existing bash entry-point or migrate later. |
| `teardown.sh` is bash-only | High | `scripts/teardown.sh` | Same treatment. Smaller — quicker port. |
| `mcp_config.py` may emit Unix paths | Medium | `scripts/mcp_config.py` | Add Windows-path round-trip test; ensure `command` is `<project>\.venv\Scripts\entraclaw-mcp.exe`. |
| `setup.sh` uses `/tmp` | Medium | `setup.sh:734` | Replaced by Python rewrite. |
| `chmod 600` on `.env` | Medium | `setup.sh:714` | Use `icacls` on Windows; helper module `entraclaw_setup.fs.lock_down_file()` handles both. |
| `keyring` PEM round-trip on Windows untested | Medium | `platform/windows.py` | Add a tests/integration smoke test (skipped unless `ENTRACLAW_TEST_WINDOWS_KEYRING=1`). Run on a Windows VM in CI or by hand. |
| Editable-install on Windows path with spaces (e.g., `C:\Users\Brandon Werner\…`) | Medium | Generic | The repo path on Mac is `/Volumes/Development HD/…` — already has a space — and works. Confirm on Windows. |
| Microsoft Store Python is broken for editable installs | Medium | New | Detect via `sys.base_prefix` pointing into `WindowsApps\` and refuse with a clean error. |
| `.venv\Scripts\python.exe` vs `.venv/bin/python3` | Low | `setup.sh:331,504,684,886` | Python orchestrator computes once via `sys.executable`. |
| ADR-003 claims TPM/CNG on Windows | Low (correctness debt) | `docs/decisions/003-…md:58` | Either deliver TPM-backed keys (large v2 effort) or amend the ADR to describe what we actually do today (Credential Locker, encrypted at rest by DPAPI, bound to the user profile). **Amend the ADR as part of this work.** |

---

## Recommended script design

### File layout

```
scripts/
  entraclaw_setup/
    __init__.py
    __main__.py            # python -m entraclaw_setup [setup|deploy|teardown]
    cli.py                 # argparse / typer entry points (setup/deploy/teardown)
    prereqs.py             # az/python/git presence + version checks
    azlogin.py             # az account show wrapper, --switch-user
    provisioner.py         # thin wrapper around entra_provisioning.py
    identity.py            # thin wrapper around create_entra_agent_ids.py
    cert.py                # the inline-bash cert gen+upload, lifted verbatim
    env_writer.py          # .env writer + lock_down_file()
    blob.py                # thin wrapper around provision_blob_storage.py
    mcp_wiring.py          # invokes mcp_config.py
    teardown.py            # the deletion sequence currently in teardown.sh
    fs.py                  # lock_down_file(), tempfile helpers
    ui.py                  # colored output (rich), prompts, non-interactive defaults

  setup-windows.ps1        # ~50 lines: prereq nudges, venv create, invokes python -m entraclaw_setup setup
  deploy-windows.ps1       # ~30 lines: invokes python -m entraclaw_setup deploy (= MCP wiring + final summary, no Entra changes)
  teardown-windows.ps1     # ~30 lines: invokes python -m entraclaw_setup teardown
  setup-windows.cmd        # one-liner: pwsh -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1" %*
```

### Why `setup-windows` AND `deploy-windows`?

The user asked for both. The split aligns with how Mac users actually use the existing flow:

- **setup-windows** = "make this machine able to talk as an agent": prereqs, `az login`, Entra provisioning, cert gen, `.env`, optional blob. Idempotent. Re-runs are safe.
- **deploy-windows** = "wire this machine's agent into Claude Code / Copilot CLI": MCP config writes, final summary, no Entra-side changes. This lets the user re-wire MCP after upgrading Claude Code without touching Entra.

Both call into the same Python package, so the logic is shared.

### What the PS1 wrappers actually do

`setup-windows.ps1` (illustrative — do not implement from this snippet):

```powershell
#Requires -Version 7.0
[CmdletBinding()] param(
    [string]$UpnSuffix,
    [string]$UseBlueprint,
    [switch]$New,
    [switch]$CloudMemory,
    [switch]$SwitchUser,
    [string]$TeamsUser
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Detect Microsoft Store Python and refuse
$py = (Get-Command python).Source
if ($py -like '*WindowsApps*') {
    throw "Microsoft Store Python is not supported. Install from python.org."
}

# Create / refresh venv
if (-not (Test-Path .venv)) { python -m venv .venv }
. .venv\Scripts\Activate.ps1
python -m pip install --quiet -e ".[dev,provisioning]"

# Hand off to Python orchestrator
$pyArgs = @('-m', 'entraclaw_setup', 'setup')
if ($New)         { $pyArgs += '--new' }
if ($UpnSuffix)   { $pyArgs += '--with-upn-suffix', $UpnSuffix }
# ...remaining flags...
& python @pyArgs
exit $LASTEXITCODE
```

Notably absent: any provisioning logic. The PS1 is a launcher, period.

### Should we share logic with bash setup.sh?

**Yes — by deprecating setup.sh, not by duplicating it.** Concretely:

- **Phase 1 (this work):** ship `scripts/entraclaw_setup/` and the PS1 wrappers. `setup.sh` keeps working, untouched. Mac users see no change.
- **Phase 2 (follow-up):** rewrite `setup.sh` as a 30-line bash shim that invokes `python -m entraclaw_setup setup`. Single source of truth. This step needs its own go/no-go because it changes Mac behavior.

This avoids a long-lived two-implementations problem and keeps every learning (`#29` shell-capture corruption, `#34` storage consent, `#36` worktree venv) fixed in one place.

---

## Implementation phases

### Phase 0 — Prep (no user-visible change)

1. Add `tests/test_setup_orchestrator.py` that mocks `subprocess.run` and Graph endpoints. This is the regression net before refactoring.
2. Add `tests/test_platform_windows_keyring.py` with a real PEM round-trip, gated on `sys.platform == 'win32'` and skipped elsewhere.
3. Amend ADR-003 to describe today's reality (Credential Locker / Keychain login keychain; not TPM/Secure-Enclave-bound). File ADR-007 if/when we add CNG.

### Phase 1 — Extract Python orchestrator

Files changed/added:
- `scripts/entraclaw_setup/` (new package, ~600 lines net after extracting from setup.sh)
- `scripts/setup-windows.ps1`, `scripts/deploy-windows.ps1`, `scripts/teardown-windows.ps1`, `scripts/setup-windows.cmd` (new, ~120 lines total)
- `pyproject.toml`: add `[project.scripts] entraclaw-setup = "entraclaw_setup.cli:main"` so users can also run `entraclaw-setup setup` once the venv is active.
- `tests/test_setup_orchestrator.py`, `tests/test_setup_windows_paths.py`, `tests/test_mcp_config_windows.py`

Acceptance:
- All existing tests pass on macOS.
- New test suite passes on macOS via mocks.
- A Windows VM (clean Windows 11, only Python + az + git installed) runs `scripts\setup-windows.cmd --new --with-upn-suffix=winagent` to a green summary.

### Phase 2 — Hardening

- `keyring` size sanity check + automatic fallback if the platform can't store a 2048-bit PEM.
- NTFS ACL `.env` lockdown via `icacls`, with a Python fallback using `os.chmod` (which sets read-only attribute, not ACLs) if `icacls` is missing.
- Detect WSL and refuse OR offer to call out to PowerShell — running setup inside WSL but storing a key in the WSL `keyring` produces a key that `entraclaw-mcp.exe` running on Windows host won't see. Document clearly.

### Phase 3 — Rewrite setup.sh as a shim (optional, separate go-decision)

- `setup.sh` becomes 30 lines: load `.entraclaw-state.json`, activate venv, exec `python -m entraclaw_setup setup "$@"`.
- Same for `teardown.sh`.

### Phase 4 — TPM / CNG (separate v2, not blocking parity)

See **Certificate storage design** below.

---

## Certificate storage design (Windows)

### v1 (recommended for parity ship): `keyring` + Windows Credential Locker

This is what `platform/windows.py` already does. The PEM lives in `Windows Credential Manager` under `service="entraclaw"`, `target="blueprint-private-key"`. DPAPI encrypts the value at rest, scoped to the current Windows user profile. The key cannot be extracted by another local user without the user's password (or admin + DPAPI master-key access).

**Pros:** zero new code, identical to Mac's Keychain story. Honest about its security profile.
**Cons:** key is exfiltratable by malware running as the user. Same constraint as Mac's login keychain when the screen is unlocked.

**Action items:**
- Verify PEM round-trip with a real 2048-bit RSA PEM (~1.7 KB) on Windows 11.
- Document the security profile honestly in ADR-003.

### v2 (future): Windows Certificate Store + CNG, optionally TPM-backed

Promote the private key into `cert:\CurrentUser\My` via `New-SelfSignedCertificate -KeyAlgorithm RSA -KeyLength 2048 -CertStoreLocation Cert:\CurrentUser\My -KeyExportPolicy NonExportable -Provider "Microsoft Platform Crypto Provider"`. The `Microsoft Platform Crypto Provider` keys are TPM-bound and non-exportable; signing operations go through the TPM.

For the JWT assertion, swap `cryptography.hazmat.primitives.asymmetric.rsa.sign` for a thin Windows-native shim:
- Either call PowerShell (`Get-PfxCertificate` style — but for cert store, use `[System.Security.Cryptography.X509Certificates.X509Store]`) and have it sign via `RSACng.SignData`.
- Or use the `python-pkcs11` / `oscrypto` route to talk to CNG/MS Platform KSP directly from Python.
- Or invoke a small C# helper compiled once as a single-file `.exe` shipped alongside the Python package.

**Effort:** real. The signing call is replaced; everything downstream (the JWT base64 encoding, the Graph upload of the public cert) stays the same. New `WindowsCngCertStore` class implements a credential-store-shaped interface with `sign_assertion()` instead of `retrieve()` returning a PEM. This requires a `CredentialStore` protocol extension (or a new `CertStore` protocol).

**Recommendation:** ship v1 first. Land Mac parity. Then evaluate whether the security uplift is worth the complexity. If yes, do it for Mac (Secure Enclave) at the same time so ADR-003's claim becomes true on both platforms simultaneously.

### Fallback if v1 is too constrained

If the PEM-in-Credential-Locker round-trip fails (size limit, encoding bug, future Microsoft change), the immediate fallback is to write the PEM to `%LOCALAPPDATA%\entraclaw\blueprint-private-key.pem` with `icacls` ACL locking it to the current user, encrypted via DPAPI (`win32crypt.CryptProtectData`). This is functionally equivalent to today's Mac flow at a lower abstraction level. Document the choice in code; do not silently change storage backends.

---

## Security model (must be true on Windows)

1. **No client secrets ever.** Cert-auth-only, exactly per ADR-003. Same on every platform.
2. **No tokens in logs.** Already enforced via `__repr__` overrides. Verify on Windows by grepping `~/.entraclaw/logs/entraclaw.log` after a test run for the substring `eyJ` (start of any JWT).
3. **Private key never on disk in plaintext** in v1's recommended path — it lives in DPAPI-encrypted Credential Locker.
4. **`.env` ACL-locked** to the current user via `icacls /inheritance:r /grant:r`. Equivalent to `chmod 600` on Mac.
5. **Audit-first:** every Teams/Graph action calls `audit/` before returning. Already cross-platform; no Windows-specific work.
6. **No Azure CLI tokens for Agent Identity APIs.** Learning #1. The provisioner app's cert-JWT path is already Windows-portable.
7. **Sponsors are users, not SPs.** Learning #5. Already enforced in `create_entra_agent_ids.py`.
8. **BlueprintPrincipal explicit creation with idempotent skip.** Already correct in `ensure_blueprint_principal()`.
9. **No fake passwords on Agent users.** Already correct.
10. **MS Store Python refusal** to avoid the "everything seems to work but `pip install -e .` silently writes to `%LOCALAPPDATA%\Packages\…`" trap.

---

## Test plan

### Unit tests (run on macOS / Linux CI — no Windows VM needed)

- `tests/test_setup_orchestrator.py` — mock `subprocess.run` for `az`, `httpx` for Graph; assert step ordering, idempotency, error-on-missing-prereqs.
- `tests/test_setup_windows_paths.py` — feed `pathlib.PureWindowsPath` instances through `mcp_wiring.py`, `env_writer.py`, `fs.lock_down_file()`. Assert backslash separators in `.mcp.json`'s `command` field, drive letters preserved.
- `tests/test_platform_windows_keyring.py::test_pem_round_trip` — skipped unless `sys.platform == 'win32'`; on Windows, generates a 2048-bit RSA key, stores PEM via `WindowsCredentialStore.store`, retrieves, parses with `cryptography`, signs+verifies a JWT to round-trip.
- `tests/test_mcp_config_windows.py` — input is a Windows-shaped project root; output `.mcp.json` deserializes cleanly with the right command path.

### Integration (manual, on a Windows VM)

A clean Windows 11 VM, bare:
1. `winget install Python.Python.3.12 Microsoft.AzureCLI Git.Git Microsoft.PowerShell`
2. New shell, `az login` (interactive browser).
3. `git clone <repo>; cd entraclaw-identity-research`
4. `.\scripts\setup-windows.cmd --new --with-upn-suffix=wintest1`
5. Expect green "Setup complete" with Blueprint/Agent ID/Agent User IDs printed.
6. `Get-Content .env` — has `ENTRACLAW_*` keys, no secrets, ACL: user-only-read (`(Get-Acl .env).Access | Format-List`).
7. `python -c "from entraclaw.platform import get_credential_store; s = get_credential_store(); print(bool(s.retrieve('entraclaw', 'blueprint-private-key')))"` — `True`.
8. `.venv\Scripts\entraclaw-mcp.exe` starts cleanly, MCP `tools/list` returns the Teams tool set.
9. From Claude Code on the same VM, send a Teams DM to the agent's UPN — message arrives in Claude Code's channel.
10. `.\scripts\teardown-windows.ps1` removes Agent User → Agent Identity → Blueprint → Provisioner cleanly. Re-run: "Nothing to clean up."

### Acceptance criteria for "ship it"

- All unit tests green on macOS.
- The integration walkthrough above succeeds end-to-end on a Windows 11 VM **twice** (once with `--new`, once with `--use-blueprint=<the same ID>` from the prior run on a different machine, exercising the multi-machine cert path).
- ADR-003 amended to match reality.
- `setup.sh` unchanged; Mac users unaffected.

---

## Open questions and risks

1. **`keyring` PEM size on Windows Credential Locker.** Documented limits are inconsistent (some sources say 2.5 KB, some 5 KB, some unlimited via Credential Locker API). 2048-bit RSA PEM is ~1.7 KB; should be fine, but verify on real hardware before declaring victory. *Verification:* one-line script on a Windows VM.
2. **WSL/native ambiguity.** A user running `wsl` and then `python -m entraclaw_setup setup` will write to the WSL Linux keyring, not Windows. The MCP server invoked by Windows Claude Code won't find the key. *Mitigation:* PS1 wrapper aborts if `$env:WSL_DISTRO_NAME` is set, with a clean message: "Run setup-windows.ps1 from PowerShell on the Windows host, not from WSL."
3. **Azure CLI under PowerShell quoting.** The provisioning Python already shells out to `az` with explicit `subprocess.run([…])` (list-form, not shell-string). PowerShell quoting issues only arise if our PS1 passes user input as a single string. *Mitigation:* PS1 splat (`@pyArgs`) into the Python invocation.
4. **Microsoft Store Python detection.** `sys.base_prefix.lower().contains("windowsapps")` works. False-positive risk: zero (Microsoft Store Python is the only Python that lives there).
5. **Path with spaces** (e.g., `C:\Users\Brandon Werner\…`). Most-used quirk on Windows. The Python orchestrator handles this trivially; the PS1 needs `&` invocation with quoted paths. Test with `C:\Users\Test User\Code\entraclaw` explicitly.
6. **Code-signing the PS1.** Out of scope. The `.cmd` shim with `-ExecutionPolicy Bypass` is the documented way around it. Mention in README.
7. **Azure Blob provisioning identity propagation on Windows.** The `Storage Blob Data Contributor` role assignment Learning #34 + permission-propagation backoff (Learning #8) takes 30–120 s. Same on every platform. No Windows-specific concern.
8. **Multi-user Windows boxes.** `keyring` keys are per-user. Two Windows users on the same machine each need their own setup. Same as Mac. Document clearly.
9. **MCP-disconnect investigation (open dossier).** Out of scope but: ensure the JSON-rotating log handler from PR #40 works under Windows file-locking semantics. *Mitigation:* `RotatingFileHandler` is in stdlib and tested on Windows.
10. **Domain-joined / AAD-joined laptops with Conditional Access policies.** Some tenants block `client_credentials` from non-managed devices. *Verification step:* on first run, capture the exact AADSTS code from a CA-block and document the CA exception that needs to be requested. Not a blocker for the script — but a likely real-world friction point.

---

## "Can we make it just as easy as Mac?"

**Yes, with one caveat.** The flow can be one command (`scripts\setup-windows.cmd --new --with-upn-suffix=foo`) that produces the same green summary as Mac. The only Windows-specific friction we cannot eliminate is the *prerequisite install* step — the user has to install Python, Azure CLI, git, and PowerShell 7 once, the same way Mac users install Homebrew + Python + az. We can streamline that with a copy-pasteable `winget install` one-liner in the README, which is the platform-idiomatic equivalent of `brew install`.

After prereqs are present, **the user-experience difference between Mac and Windows is exactly one character**: `./scripts/setup.sh` vs `.\scripts\setup-windows.cmd`. Behind the scenes both call the same Python orchestrator and produce identical Entra state.

The one place we *do not* hit parity in v1 is the security claim about TPM-backed keys. Today's `keyring` path is functionally equivalent on Mac and Windows (Keychain ↔ Credential Locker, both DPAPI-class user-scoped encryption), but neither is hardware-bound. ADR-003 should be amended to match reality, and a v2 ADR can plan a coordinated Mac-Secure-Enclave + Windows-CNG-TPM upgrade if the threat model demands it.

**Bottom line:** one Windows VM for acceptance testing, no protocol changes, no new dependencies. The bash-to-Python extraction is the only real work; everything else is wrapping and verification.
