# Plan: Windows Port (host-agnostic, lean scope)

> Supersedes `docs/architecture/next-windows-dev-environment.md` (2026-04-05),
> `docs/openai-windows-agent-identity-port.md` (2026-04-24), and
> `docs/claude-windows-port.md` (2026-04-28). Those three docs were built off
> each other and disagree on two axes (orchestrator language, keystore). This
> plan picks one answer for each and frames the work as porting **the repo**
> — both Copilot CLI and Claude Code are first-class hosts.
>
> **Scope decision (2026-04-28, /plan-eng-review D1):** ship Windows in ~6
> files by writing `setup-windows.ps1` directly against the existing
> cross-platform Python helpers. Do NOT refactor `setup.sh` (1,032 lines of
> bash) into a Python orchestrator package as part of this work — that was
> rejected as scope creep. A future "unify Mac/Linux/Windows on one
> orchestrator" is its own project, evaluated on its own merits when there
> is real evidence the bash↔PowerShell duplication is causing pain. See
> `PLAN-windows-port.md.v1-bak` for the rejected fuller plan.

## Problem

Today the three-hop Agent User flow ships only on macOS (and de-facto Linux):
`scripts/setup.sh` is bash, the Blueprint private key lives in macOS Keychain
via `keyring`, and `~/.entrabot/` uses a dot-prefix path that is non-idiomatic
on Windows. We want one-command UX on Windows — `setup-windows` provisions a
fresh device, `deploy-windows` re-mints the cert and refreshes registration —
that wires both Copilot CLI (`~/.copilot/mcp-config.json`) and Claude Code
(`.mcp.json`) without the user knowing the difference.

## What already exists (Step 0 finding)

The work below leans on cross-platform Python helpers that already ship and are
already used by `setup.sh` via `python3 ...`:

- `scripts/entra_provisioning.py` (658 lines) — Blueprint + AgentIdentity + sponsors.
- `scripts/create_entra_agent_ids.py` (1055 lines) — Agent User provisioning, FIC, consent.
- `scripts/provision_blob_storage.py` (250 lines) — RG + storage account + RBAC.
- `scripts/mcp_config.py` (158 lines) — writes BOTH `.mcp.json` AND
  `~/.copilot/mcp-config.json` with the right binary path (verified at
  `tests/scripts/test_mcp_config.py:43-374`).

`setup.sh` itself (1,032 lines) is bash orchestration around those Python
helpers. We do NOT touch it as part of this work. The Windows port writes a
parallel PowerShell orchestrator that calls the same Python helpers.

## Approach

Two structural decisions, locked in:

### Decision A — Orchestrator: PowerShell calling existing Python helpers

`scripts/setup-windows.ps1` (~250 lines) plays the role bash `setup.sh` plays
on Mac/Linux: prereq probes, prompts, env-var wiring, then `python -m`
invocations of the existing helpers. **The PS1 is parallel to bash, not a
replacement for bash.** Mac/Linux developers continue using `setup.sh`
unchanged; Windows developers use `setup-windows.ps1`.

The only genuinely new orchestration code is `scripts/generate_windows_cert.py`
— a single-purpose helper that wraps `New-SelfSignedCertificate` via subprocess
and returns a thumbprint. Cert generation is the one task on Windows that
diverges meaningfully from the Mac path (Keychain PEM gen) and warrants its
own helper rather than being inlined into PS1.

Rejected alternative: extracting `setup.sh` into a `scripts/entrabot_setup/`
Python package. That was the original v1 proposal — see `.v1-bak` for the
file layout. Rejected because it bundles "port to Windows" with "rewrite
1,032 lines of bash" and the second is a separate refactor that should stand
on its own merits, not ride on this PR.

### Decision B — Keystore: TPM-first, software fallback (per user)

At setup time the script checks `Get-Tpm`. If `TpmReady` is true, generate the
Blueprint cert with `New-SelfSignedCertificate -Provider 'Microsoft Platform
Crypto Provider' -KeyExportPolicy NonExportable -CertStoreLocation
Cert:\CurrentUser\My`. Private key never leaves the TPM. If TPM is not ready,
fall back to the same `New-SelfSignedCertificate` call with
`-Provider 'Microsoft Software Key Storage Provider'` — DPAPI-encrypted at
rest, bound to the user profile. Both end up in `Cert:\CurrentUser\My`; the
runtime signer does not care which KSP is behind it.

Either path is **at least as strong as the Mac baseline** (Mac uses Keychain
PEM, software-bound to the login keychain). The TPM path is strictly stronger;
the software-KSP path is roughly Mac-equivalent (DPAPI ≈ Keychain). Fallback
is silent-but-logged — the `.env` records `ENTRABOT_BLUEPRINT_KSP=tpm|software`
so triage can tell the two apart.

The runtime change isolates to a new `auth/cncrypt_signer.py` that calls
`ncrypt.dll` via `ctypes` (`CryptAcquireCertificatePrivateKey` →
`NCryptSignHash` with `BCRYPT_PKCS1_PADDING_INFO{pszAlgId="SHA256"}`). The
existing `build_client_assertion` interface stays unchanged for callers; only
the platform branch picks the signer.

## Other decisions (unchanged from the source docs)

- **Cert generation:** `New-SelfSignedCertificate`, never ship `openssl` on Windows.
- **`az` parsing:** `-o json | ConvertFrom-Json` everywhere — never TSV (Learning #7).
- **Thumbprint capture:** Python helper validates with regex `^[A-F0-9]{40}$` and rejects empty/multi-line stdout (Learning #29).
- **`.env` lockdown:** `icacls .env /inheritance:r /grant:r "$env:USERNAME:M"` — modify (read+write+delete-self), strips inherited ACLs. Matches `chmod 600` semantics; allows setup re-runs and rotation to update `.env` (eng-review finding D10, codex tension #3).
- **Path conventions:** `%LOCALAPPDATA%\entrabot\` on Windows; keep `~/.entrabot/` everywhere else. One change in `_default_dir` covers all six call sites. **Migration is one-shot at setup time, not lazy at every read** (eng-review finding D2). PLUS: runtime guard in `config.py` — at MCP boot, if legacy `~/.entrabot\` exists with content while target `%LOCALAPPDATA%\entrabot\` is empty/missing, fail loud with "run setup-windows.cmd --migrate" (eng-review finding D11, codex tension #4).
- **Cert-gen crypto params (Windows):** `generate_windows_cert.py` invokes `New-SelfSignedCertificate` with `-KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -KeyUsage DigitalSignature -KeySpec Signature` explicitly — never trust defaults, which have shifted across Windows builds and would silently produce a cert the cncrypt_signer (PKCS1+SHA256) cannot use (eng-review finding D9, codex tension #2).
- **Token cache:** `msal_extensions.build_encrypted_persistence` already uses DPAPI on Windows. No change.
- **Microsoft Store Python:** detect via `sys.base_prefix` containing `WindowsApps`, refuse with a clean error.
- **PowerShell version:** target 7.x; warn on 5.1. Distribute via `setup-windows.cmd` (one-liner: `pwsh -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1" %*`) so the user never has to touch `Set-ExecutionPolicy`.
- **AppContainer / sandbox:** out of scope for v1. File layout (`%LOCALAPPDATA%\entrabot\`, no `%PROGRAMFILES%` writes, no admin elevation) leaves the door open for an MSIX wrapper later.
- **MCP wiring:** `scripts/mcp_config.py` already writes both `.mcp.json` (Claude Code) and `~/.copilot/mcp-config.json` (Copilot CLI). On Windows the binary path becomes `<project>\.venv\Scripts\entrabot-mcp.exe`. Add a Windows-path round-trip test.
- **WSL detection:** if `setup-windows.ps1` somehow gets invoked from inside WSL, refuse with a clear message that **native-Windows entrabot must be set up from native PowerShell**. WSL itself is fine — that's the Linux path, run `./scripts/setup.sh` from WSL instead.
- **ADR-003:** amend to describe what we actually do per platform — Keychain (Mac), Cert Store + CNG with TPM-or-software KSP (Windows), Secret Service (Linux).

## File layout (new + modified)

```
scripts/
  setup-windows.ps1            # NEW ~250 lines: prereq probes, TPM probe, venv, prompts,
                               # invokes existing Python helpers (entra_provisioning.py,
                               # create_entra_agent_ids.py, provision_blob_storage.py,
                               # mcp_config.py) plus the new generate_windows_cert.py.
  deploy-windows.ps1           # NEW ~80 lines: cert rotation w/ smoke-test gate before
                               # deleting old cert from Cert:\CurrentUser\My.
  teardown-windows.ps1         # NEW ~50 lines: undo of setup.
  setup-windows.cmd            # NEW one-liner: pwsh -ExecutionPolicy Bypass -File ...
  generate_windows_cert.py     # NEW ~150 lines: subprocess wrapper around
                               # New-SelfSignedCertificate with hard-locked crypto params
                               # (-KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256
                               # -KeyUsage DigitalSignature -KeySpec Signature).
                               # TPM-first/software-fallback. Returns thumbprint with regex
                               # validation; exports public cert DER for Graph upload.
  rotate_cert_windows.py       # NEW ~120 lines: rotation logic extracted from
                               # deploy-windows.ps1 so pytest can drive it. Captures old DER,
                               # PATCHes new DER, runs smoke test, on failure: re-PATCHes old
                               # DER, restores .env, invalidates MSAL cache, halts.

src/entrabot/
  auth/
    certificate.py             # MODIFIED: dispatch by sys.platform — Mac/Linux call
                               # existing PEM signer; Windows calls cncrypt_signer.
    cncrypt_signer.py          # NEW ~100 lines: ctypes ncrypt.dll signer for
                               # non-exportable CNG keys. Identical signature to existing
                               # PEM signer; KSP choice (TPM vs software) is invisible.
  platform/
    windows.py                 # REWRITE (current 22-line stub): thumbprint-based lookup
                               # against Cert:\CurrentUser\My, no PEM round-trip.
  config.py                    # MODIFIED: _default_dir consults %LOCALAPPDATA% on Windows;
                               # one-shot migration helper called from setup-windows.ps1
                               # (NOT lazily on every read); PLUS startup runtime guard that
                               # halts loud if legacy ~/.entrabot\ has content while target
                               # is empty (D11).

tests/
  test_cncrypt_signer.py       # NEW: mock-based tests for ctypes binding (NTSTATUS handling,
                               # padding-info struct layout, error path); always-runs.
  test_certificate_windows.py  # NEW: gated on sys.platform == 'win32'; smoke-tests the
                               # full TPM/software signer end-to-end against a real cert.
  test_platform_windows.py     # NEW: thumbprint lookup tests; always-runs (mocks Cert: store).
  test_config_windows_path.py  # NEW: %LOCALAPPDATA% resolution + migration helper +
                               # runtime-guard for unmigrated state; always-runs.
  test_deploy_rollback.py      # NEW (REGRESSION-CRITICAL, D7): drives rotate_cert_windows.py
                               # with mocked Graph PATCH + smoke-test outcomes. Asserts:
                               # (a) PATCH ok + smoke ok = no rollback;
                               # (b) PATCH ok + smoke fail = re-PATCH original DER + .env
                               #     restore + MSAL cache invalidation + halt;
                               # (c) Initial PATCH fails = no rollback needed.
  test_generate_windows_cert.py # NEW: mocks subprocess.run; verifies the hard-locked crypto
                                # params (D9) appear verbatim in the New-SelfSignedCertificate
                                # invocation; thumbprint regex rejection of malformed stdout.

.github/workflows/
  test-windows.yml             # NEW: pytest on windows-latest runner. Eng-review finding
                               # D3 — without this, the entire Windows port rots silently.

docs/
  decisions/003-certificate-auth-over-client-secrets.md   # AMEND: per-platform reality.
  runbooks/windows-setup.md                                # NEW: Windows runbook;
                                                            # consolidates the three
                                                            # superseded docs.
```

**Total: 7 new src/scripts files + 7 new test files + 1 CI workflow + 2 modified files + 1 ADR amendment.**

## Phases

### Phase 1 — Windows path (single PR if it stays small enough)

- New PS1 + CMD wrappers (`setup-windows.ps1`, `deploy-windows.ps1`, `teardown-windows.ps1`, `setup-windows.cmd`).
- New `scripts/generate_windows_cert.py` with hard-locked crypto params (D9).
- New `scripts/rotate_cert_windows.py` — Python helper called by `deploy-windows.ps1` for the rotation logic (Graph PATCH, smoke test, rollback). Extracted from PS1 so the regression test is testable from pytest (D7).
- New `auth/cncrypt_signer.py` with TPM-first / software-fallback signer.
- Rewrite `src/entrabot/platform/windows.py` to use Cert: store thumbprint lookups.
- `config.py`: `_default_dir` → `%LOCALAPPDATA%\entrabot\` on Windows + one-shot migration helper + **runtime guard** that fails loud if legacy dir is non-empty post-migration (D11).
- `.github/workflows/test-windows.yml` — `pytest` on `windows-latest`. Mandatory, NOT a follow-up.
- Tests: cncrypt_signer (mock-based, always-runs), platform/windows (mock-based, always-runs), config Windows path + migration + runtime-guard (always-runs), certificate_windows (gated on win32, runs in CI), **`test_deploy_rollback.py` — REGRESSION-CRITICAL test for cert rotation rollback (D7)**, generate_windows_cert (mocks subprocess; verifies hard-locked crypto params land in the New-SelfSignedCertificate call).

**Acceptance:**
- All existing tests pass on macOS.
- New tests pass on macOS (mock-based) AND on `windows-latest` GitHub runner (full suite).
- Clean Windows 11 VM (only Python + az + git installed): `scripts\setup-windows.cmd --new --with-upn-suffix=winagent` runs to a green summary, three-hop token flow succeeds, `send_teams_message` round-trips.
- **Both hosts boot:** Copilot CLI AND Claude Code MCP wiring verified by booting each host in the same project and listing tools.
- **Long-running runtime exercised** (eng-review finding D6): on the Windows VM, leave `entrabot-mcp.exe` running for 5 minutes, send a Teams DM from another account, confirm the channel-push notification fires AND `wait_for_sponsor_dm` blocks then wakes correctly.
- TPM path tested on a TPM-2.0 box (Azure VM with vTPM); software-fallback path tested on a TPM-disabled VM.

### Phase 2 — Hardening (separate, smaller PR)

- WSL detection in `setup-windows.ps1` (clean refusal with redirect to `setup.sh`).
- Docs: `docs/runbooks/windows-setup.md` (replaces the three superseded docs once Phase 1 lands).
- `keyring` size sanity check + automatic fallback diagnostic if the Mac/Linux path can't store a 2048-bit PEM (Mac/Linux only — Windows is past this since it stops using `keyring` for the cert key).

(Note: `deploy-windows.ps1` rotation flow + rollback regression test were originally Phase 2 but pulled into Phase 1 per eng-review D12 / codex tension #6 — the stated promise of "one-command UX with deploy-windows" should match the shipped scope.)

### Rotation rollback contract (deploy-windows.ps1)

Three-step transactional rollback when smoke test fails post-Graph-PATCH:

1. Re-PATCH the original public DER bytes back to the Agent Identity (capture old DER BEFORE the new PATCH — non-exportable TPM keys cannot be recovered if missed).
2. Restore previous thumbprint in `.env` (and re-apply `:M` icacls).
3. **Invalidate MSAL token cache** (`%LOCALAPPDATA%\entrabot\.msal-cache.bin`) — otherwise tokens issued under the now-invalid new public key cause a 401 storm on next call (eng-review D13, codex tension #9).

Old cert is NOT deleted from `Cert:\CurrentUser\My` until smoke succeeds. On rollback, halt with explicit user-facing error.

## Open questions (flag, do not silently decide)

1. **TPM probe admin requirement** (eng-review D4) — `Get-Tpm` may require admin in some configurations. Verify on a non-elevated PowerShell 7 session before locking it in. Alt: `(Get-CimInstance -ClassName Win32_Tpm -Namespace root\CIMV2\Security\MicrosoftTpm).IsEnabled_InitialValue`.
2. **Cert rotation cadence** — 365 days today. Worth dropping to 90 once `deploy-windows.ps1` is automated and we trust the smoke-test gate? Defer to Phase 2.
3. **Future "unify orchestrators" project** — not part of this plan. Capture as a TODO once we have Windows shipping and can measure how often we have to fix the same learning in two places.

## Out of scope (call out)

- **Refactoring `setup.sh` into Python.** Originally bundled with this plan; descoped at /plan-eng-review D1. Re-evaluate later when there's real evidence of pain.
- AppContainer / Win32 app isolation / MSIX packaging — file layout is friendly, but the wrapping work is its own project.
- Linux native cert-store (e.g., `gnome-keyring` PKCS#11) — current Linux path uses `keyring` Secret Service for PEM; that's the Mac-equivalent baseline and we keep it.
- Bot Gateway port — separate plan; Bot Framework SDK already runs cross-platform.
- WSL setup support — Linux entrabot works under WSL today via the existing `setup.sh`. The Windows port targets native-Windows.

## Failure modes (per new codepath)

| Codepath | Failure | Tested? | Handled? | User sees |
|---|---|---|---|---|
| `cncrypt_signer.sign_pkcs1_sha256` | NCryptSignHash NTSTATUS != 0 | ✅ unit | ✅ raise | Clear "signing failed: <NTSTATUS>" error, halts |
| `cncrypt_signer` buffer-too-small | NTE_BUFFER_TOO_SMALL on first call | ✅ unit | ✅ retry | Transparent, retry succeeds |
| `generate_windows_cert` | New-SelfSignedCertificate stdout corruption | ✅ unit (regex) | ✅ reject | Clear "thumbprint validation failed" error |
| `generate_windows_cert` | TPM provider unavailable mid-run | ⚠️ manual | ✅ fall back | Logged "TPM not ready, using software KSP" |
| `rotate_cert_windows` | Smoke test fails post-PATCH | ✅ regression (D7) | ✅ rollback | Clear "rotation rolled back, original cert restored" |
| `rotate_cert_windows` | Rollback PATCH itself fails | ✅ regression (D7) | ⚠️ halt-loud | "MANUAL INTERVENTION: rollback PATCH failed, agent identity may be in inconsistent state" |
| `config._default_dir` | Both legacy and target dir non-empty | ✅ unit | ✅ halt | "two entrabot dirs detected, manual triage needed" |
| `config` startup guard | Legacy dir non-empty, target empty | ✅ unit | ✅ halt | "run setup-windows.cmd --migrate" |
| `platform/windows.py` lookup | Thumbprint missing from Cert: store | ✅ unit | ✅ raise | Clear "cert not found: <thumbprint>" |
| MSAL cache invalidation post-rollback | Cache file locked by another process | ⚠️ manual | ✅ retry+log | Warning, next call re-mints clean |

**Critical gaps:** None. The "rollback PATCH itself fails" case halts loud — manual triage is the right response when both Graph PATCHes fail in a row (network outage; Brandon would want to know, not auto-recover).

## Worktree parallelization strategy

**Sequential — no parallelization opportunity.** All Phase 1 work touches the same Windows path:
- `auth/certificate.py` and `auth/cncrypt_signer.py` are coupled (dispatch → signer).
- `config.py` migration helper is called by both `setup-windows.ps1` and the runtime guard.
- `platform/windows.py` is consumed by certificate.py.
- `generate_windows_cert.py` and `rotate_cert_windows.py` share the same cert-DER capture pattern.

Splitting across worktrees would create merge headaches without gaining wall-clock time. One PR, sequential edits.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | not run (diff scope) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 0 unresolved, 0 critical gaps, 14 decisions locked (D1–D13 + 2 TODOs) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not applicable (CLI/MCP, no UI) |
| Outside Voice | codex-plan-review | Independent plan critique | 1 | issues_found | 12 findings; 5 substantive tensions resolved (all chose "A"), 7 framing/already-addressed |

- **CODEX:** Surfaced 5 substantive plan tensions (crypto param drift, icacls mistake, migration upgrade trap, deploy/rotation in Phase 2 vs stated promise, MSAL cache in rollback). All 5 incorporated into the plan as D9–D13. ctypes-vs-.NET signer (#1) and the ~6-files framing (#11/#12) noted but kept; relevant TODOs added for future re-evaluation.
- **CROSS-MODEL:** 5 tensions resolved with explicit user decisions; remaining 7 codex points either redundant with eng-review findings (D5/D3/D6) or framing critiques.
- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to implement.
