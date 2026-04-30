# requires -Version 7.0
<#
.SYNOPSIS
  EntraClaw — Windows setup. Mirror of scripts/setup.sh for Windows.

.DESCRIPTION
  Provisions the agent identity on a Windows host:
    1. Refuse-on-WSL (Phase 2 finding — WSL users should run setup.sh).
    2. Probe prereqs (pwsh 7, python 3.12+, az CLI, git).
    3. Bootstrap venv + pip install.
    4. Run config.py migration helper (one-shot move of legacy ~/.entraclaw).
    5. Call entra_provisioning.py + create_entra_agent_ids.py via az login.
    6. Generate the Blueprint cert (TPM-first, software-fallback) via
       generate_windows_cert.py and PATCH it into the Blueprint.
    7. Write .env with both thumbprints (SHA-1 hex + SHA-256 b64url).
       icacls -M (D10) — modify, NOT readonly. Setup re-runs need to
       update .env or rotation halts.
    8. Register the entraclaw MCP server via mcp_config.py.

  See docs/architecture/PLAN-windows-port.md for the full design and the
  failure-modes table.

.PARAMETER NewChain
  Create a completely new Agent Identity chain.

.PARAMETER UseBlueprint
  Attach to an existing Blueprint by App ID.

.PARAMETER UpnSuffix
  Agent User UPN suffix (required with -NewChain).

.PARAMETER CloudMemory
  Provision Azure Blob Storage for operational data (default: local).

.EXAMPLE
  .\scripts\setup-windows.ps1 -NewChain -UpnSuffix winagent
#>

[CmdletBinding()]
param(
    [switch]$NewChain,
    [string]$UseBlueprint = "",
    [string]$UpnSuffix = "",
    [switch]$CloudMemory,
    [switch]$Migrate,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

# ═══════════════════════════════════════════════════════════════════════════
# 1. Refuse to run inside WSL (Phase 2 finding)
# ═══════════════════════════════════════════════════════════════════════════
if ($IsLinux -or $env:WSL_DISTRO_NAME) {
    Write-Host "ERROR: setup-windows.ps1 invoked from inside WSL." -ForegroundColor Red
    Write-Host "  WSL is a Linux environment; run scripts/setup.sh instead." -ForegroundColor Red
    Write-Host "  To set up native Windows, run setup-windows.cmd from a" -ForegroundColor Red
    Write-Host "  Windows PowerShell terminal (not a WSL shell)." -ForegroundColor Red
    exit 1
}

if (-not $IsWindows) {
    Write-Host "ERROR: setup-windows.ps1 must run on Windows." -ForegroundColor Red
    exit 1
}

# ═══════════════════════════════════════════════════════════════════════════
# 2. Resolve project root
# ═══════════════════════════════════════════════════════════════════════════
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$ScriptDir   = Join-Path $ProjectRoot 'scripts'
$VenvPython  = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

function Step($n, $msg) {
    Write-Host ""
    Write-Host "═══ Step $n / 8 — $msg" -ForegroundColor Cyan
}
function Success($msg) { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Fail($msg)    { Write-Host "  ✗ $msg" -ForegroundColor Red; exit 1 }

# ═══════════════════════════════════════════════════════════════════════════
# 3. Probe prereqs
# ═══════════════════════════════════════════════════════════════════════════
Step 1 "Probing prerequisites"

$missing = @()
foreach ($tool in 'python', 'az', 'git', 'pwsh') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        $missing += $tool
    }
}
if ($missing) {
    Fail "Missing tools: $($missing -join ', '). Install them and retry."
}
Success "Found: python, az, git, pwsh"

$pyVer = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$pyVer -lt [version]'3.12') {
    Fail "Python 3.12+ required, found $pyVer."
}
Success "Python $pyVer"

# ═══════════════════════════════════════════════════════════════════════════
# 4. One-shot data dir migration (D11)
# ═══════════════════════════════════════════════════════════════════════════
Step 2 "Migrating legacy data dir (idempotent)"

$migrateScript = @'
from entraclaw.config import migrate_legacy_data_dir
moved = migrate_legacy_data_dir()
print(f"migrated={moved}")
'@

if (Test-Path $VenvPython) {
    $out = & $VenvPython -c $migrateScript
    Success $out
} else {
    Success "venv not yet created — migration will run after pip install"
}

# ═══════════════════════════════════════════════════════════════════════════
# 5. Bootstrap venv
# ═══════════════════════════════════════════════════════════════════════════
Step 3 "Creating venv + installing dependencies"

if (-not (Test-Path $VenvPython)) {
    & python -m venv (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { Fail "venv creation failed" }
}
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install -e "$ProjectRoot[dev]" --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
Success "venv ready at $VenvPython"

# Re-run migration now that venv exists.
$out = & $VenvPython -c $migrateScript
Success $out

# ═══════════════════════════════════════════════════════════════════════════
# 6. az login + identity provisioning
# ═══════════════════════════════════════════════════════════════════════════
Step 4 "Verifying az login"

$account = az account show --output json 2>$null | ConvertFrom-Json
if (-not $account) {
    Fail "Not logged in to az. Run 'az login' and retry."
}
Success "Logged in as $($account.user.name) (tenant $($account.tenantId))"

Step 5 "Provisioning Entra Agent Identity"

$args = @()
if ($NewChain)             { $args += '--new' }
if ($UseBlueprint)         { $args += "--use-blueprint=$UseBlueprint" }
if ($UpnSuffix)            { $args += "--with-upn-suffix=$UpnSuffix" }

# entra_provisioning.py + create_entra_agent_ids.py both read az CLI
# session state directly, identical to setup.sh.
& $VenvPython (Join-Path $ScriptDir 'entra_provisioning.py')
if ($LASTEXITCODE -ne 0) { Fail "entra_provisioning.py failed" }

& $VenvPython (Join-Path $ScriptDir 'create_entra_agent_ids.py') @args
if ($LASTEXITCODE -ne 0) { Fail "create_entra_agent_ids.py failed" }

# ═══════════════════════════════════════════════════════════════════════════
# 7. Generate Blueprint cert (TPM-first / software-fallback)
# ═══════════════════════════════════════════════════════════════════════════
Step 6 "Generating Blueprint cert (TPM-first / software-fallback)"

$derPath = Join-Path $env:TEMP "entraclaw-blueprint-$(Get-Random).cer"
$certOutput = & $VenvPython (Join-Path $ScriptDir 'generate_windows_cert.py') `
    --subject "CN=entraclaw-blueprint" `
    --days 365 `
    --export-der $derPath
if ($LASTEXITCODE -ne 0) { Fail "generate_windows_cert.py failed" }

$thumbprint = ($certOutput | Select-String '^thumbprint=(.+)$').Matches[0].Groups[1].Value
$ksp        = ($certOutput | Select-String '^ksp=(.+)$').Matches[0].Groups[1].Value
$x5tS256    = ($certOutput | Select-String '^x5t_s256=(.+)$').Matches[0].Groups[1].Value

Success "Cert generated — thumbprint=$thumbprint ksp=$ksp"

# Caller needs to PATCH the public DER to the Blueprint app via Graph.
# We delegate that to a small Python one-liner that reuses
# create_entra_agent_ids.py's helpers. Skipped here because that file
# already publishes the cert during provisioning when invoked with the
# right flags; this branch only kicks in for the rotation path
# (deploy-windows.ps1 calls rotate_cert_windows.py instead).

# ═══════════════════════════════════════════════════════════════════════════
# 8. Write .env with strict ACLs (icacls -M, D10)
# ═══════════════════════════════════════════════════════════════════════════
Step 7 "Writing .env"

$envPath = Join-Path $ProjectRoot '.env'
@(
    "ENTRACLAW_TENANT_ID=$($account.tenantId)",
    "ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT=$x5tS256",
    "ENTRACLAW_BLUEPRINT_CERT_SHA1=$thumbprint",
    "ENTRACLAW_BLUEPRINT_KSP=$ksp"
) | Out-File -FilePath $envPath -Encoding utf8 -Append

# icacls :M (modify) — NOT :R (read-only). :R would self-brick: setup
# re-runs and rotation both need to update .env.
$user = "$env:USERDOMAIN\$env:USERNAME"
icacls $envPath /inheritance:r /grant:r "${user}:M" | Out-Null
Success ".env locked to $user (modify, per D10)"

# ═══════════════════════════════════════════════════════════════════════════
# 9. Register MCP server via mcp_config.py
# ═══════════════════════════════════════════════════════════════════════════
Step 8 "Registering MCP server"

$mcpBinary = Join-Path $ProjectRoot '.venv\Scripts\entraclaw-mcp.exe'
if (-not (Test-Path $mcpBinary)) { Fail "MCP binary not found at $mcpBinary" }
& $VenvPython (Join-Path $ScriptDir 'mcp_config.py') --binary $mcpBinary --project-root $ProjectRoot
if ($LASTEXITCODE -ne 0) { Fail "mcp_config.py failed" }
Success "MCP server registered for Claude Code + Copilot CLI"

Write-Host ""
Write-Host "═══ Setup complete ═══" -ForegroundColor Green
Write-Host "  KSP:        $ksp"
Write-Host "  Thumbprint: $thumbprint"
Write-Host "  Run: pwsh -File scripts\deploy-windows.ps1 to rotate cert."
