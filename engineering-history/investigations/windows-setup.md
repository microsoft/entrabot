# Windows setup runbook

> This page supersedes earlier exploratory Windows porting drafts,
> including `docs/architecture/next-windows-dev-environment.md`. The
> implementation those drafts discussed shipped via PR landing the
> `feat/windows-port` branch — see `docs/architecture/PLAN-windows-port.md`.

## Prerequisites

- **Windows 10 21H2+ or Windows 11.** Server SKUs work too but are not the
  primary target.
- **PowerShell 7+** (`pwsh`). The shipped scripts use `#requires -Version
  7.0`. Windows PowerShell 5.1 is not enough.
- **Python 3.12+** on PATH.
- **Azure CLI 2.55+** with a logged-in account that has at least
  `Application Administrator` in the target tenant (same RBAC as
  `setup.sh` on Mac).
- **git** on PATH.

Optional but recommended:

- **TPM 2.0** (BitLocker-ready). If the TPM is provisioned and reports
  `TpmReady`, setup uses the **Microsoft Platform Crypto Provider**
  (KSP=tpm) so the Blueprint private key is non-exportable and bound to
  the device. Without a TPM, setup falls back to **Microsoft Software
  Key Storage Provider** (KSP=software) — DPAPI binds the key to the
  user profile.

## One-shot setup

From a Windows PowerShell terminal (NOT a WSL shell):

```powershell
cd C:\path\to\entrabot-identity-research
.\scripts\setup-windows.cmd --new --with-upn-suffix=winagent
```

The `.cmd` wrapper elevates to `pwsh -ExecutionPolicy Bypass` so you
don't need to fight Windows policy gates per-session.

What it does:

1. **Refuses if invoked from inside WSL.** WSL is Linux; use
   `scripts/setup.sh` there instead.
2. Probes prereqs (`python`, `az`, `git`, `pwsh`).
3. Runs the **idempotent legacy data migration** —
   `~/.entrabot/` → `%LOCALAPPDATA%\entrabot\`. If both
   directories already have content, setup halts and asks you to
   triage manually (don't bypass — see Failure Modes below).
4. Creates a venv at `.venv\Scripts\python.exe`.
5. Calls `entra_provisioning.py` then `create_entra_agent_ids.py`
   (same Python helpers as `setup.sh`) to provision the Entra app
   chain.
6. Runs `generate_windows_cert.py` — auto-probes the TPM and
   generates the Blueprint cert with hard-locked crypto:
   `RSA-2048`, `SHA256`, `DigitalSignature`, `Signature`. Falls
   back to software KSP if the TPM is missing/disabled.
7. Writes `.env` and locks it with `icacls /grant:r {USER}:M`
   (modify, NOT readonly — rotation needs to update it).
8. Registers the entrabot MCP server via `mcp_config.py` for
   both Claude Code and Copilot CLI.

Final summary lists the chosen KSP and the cert SHA-1 so you can
verify both paths were honored.

## Smoke test

```powershell
.venv\Scripts\python.exe -c "from entrabot.tools.teams import acquire_agent_user_token; from entrabot.config import get_config; print('token len=', len(acquire_agent_user_token(get_config())))"
```

A green print means the three-hop flow worked end-to-end on this
machine.

## Cert rotation

Run when the cert is approaching expiry, or when the host changes
(new TPM, new user profile). Same flow as Mac's deploy script:

```powershell
.\scripts\deploy-windows.ps1
```

Contract:

- Captures the **current cert's public DER bytes BEFORE** generating
  the new cert. For non-exportable TPM keys, this is the only chance
  to grab them — without this, rollback is impossible.
- Generates the new cert.
- PATCHes the new DER to the Blueprint app via Graph.
- Runs a smoke test (acquires a fresh agent-user token).
- On success: deletes the old cert from `Cert:\CurrentUser\My`.
- On smoke failure: re-PATCHes the old DER, restores `.env`,
  invalidates the MSAL cache (`%LOCALAPPDATA%\entrabot\.msal-cache.bin`),
  raises `RotationRolledBack`.
- On rollback PATCH also failing: raises `ManualInterventionRequired`
  — triage required before next agent run.

## Teardown

```powershell
.\scripts\teardown-windows.ps1
```

Removes local cert(s), `%LOCALAPPDATA%\entrabot\`, BLUEPRINT_CERT_*
lines from `.env`, MSAL cache, and the MCP registration. Does NOT
delete the Entra app registrations from the tenant — those are
shared and persistent.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `setup-windows.ps1 invoked from inside WSL` | running setup from a WSL shell | run from native PowerShell, or use `setup.sh` for the Linux path |
| `Legacy entrabot data found at ~\.entrabot but target ... is empty` | partial migration on an older entrabot install | run setup again — migration is idempotent — or move the legacy dir manually |
| `two entrabot dirs detected: legacy ~\.entrabot and current %LOCALAPPDATA%\entrabot both contain data` | parallel installs landed both | pick one: usually the `%LOCALAPPDATA%` copy is newer; remove `~\.entrabot` |
| `New-SelfSignedCertificate failed` | provider name typo, KSP missing, or PowerShell-down-version | confirm `pwsh -Version` ≥ 7; if TPM-disabled host, force software KSP via `--ksp software` |
| `thumbprint validation failed — not 40 hex chars` | `az` warning corrupted stdout (Learning #29 cousin) | re-run setup; if it persists, file a bug with the captured stdout |
| `Initial Graph PATCH failed` (rotation) | tenant RBAC, expired Blueprint, or network | check `az account show`; the old cert is untouched, no rollback needed |
| `MANUAL INTERVENTION: rollback PATCH failed` | both new + rollback PATCH failed | re-PATCH the old DER (in `%TEMP%\entrabot-old-*.cer`) by hand via Graph PATCH |
| `entrabot-mcp.exe` exits silently mid-run | likely the same MCP-disconnect bug under investigation on Mac | see `docs/runbooks/mcp-disconnect-investigation.md` |

## What's intentionally not here

- **WSL2 entrabot.** WSL inherits the Linux setup verbatim — run
  `scripts/setup.sh` from inside WSL. The two paths don't share state.
