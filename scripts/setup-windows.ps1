<#
.SYNOPSIS
  EntraBot — Windows setup. Mirror of scripts/setup.sh for Windows.

.DESCRIPTION
  Provisions the agent identity on a Windows host:
    1. Refuse-on-WSL (Phase 2 finding — WSL users should run setup.sh).
    2. Probe prereqs (pwsh 7, python 3.12+, az CLI, git).
    3. Bootstrap venv + pip install.
    4. Run config.py migration helper (one-shot move of legacy ~/.entrabot).
    5. Call entra_provisioning.py + create_entra_agent_ids.py via az login.
    6. Generate the Blueprint cert (TPM-first, software-fallback) via
       generate_windows_cert.py and PATCH it into the Blueprint.
    7. Write .env with both thumbprints (SHA-1 hex + SHA-256 b64url).
       icacls -M (D10) — modify, NOT readonly. Setup re-runs need to
       update .env or rotation halts.
    8. Register the entrabot MCP server via mcp_config.py.

  See docs/architecture/PLAN-windows-port.md for the full design and the
  failure-modes table.

.PARAMETER NewChain
  Create a completely new Agent Identity chain.

.PARAMETER UseBlueprint
  Attach to an existing Blueprint by App ID.

.PARAMETER UpnSuffix
  Agent User UPN suffix (required with -NewChain). Also supported with
  -UseBlueprint to select an existing suffixed Agent User.

.PARAMETER AgentUserUpn
  Explicit existing Agent User UPN to reuse with -UseBlueprint, e.g.
  entrabot-agent-sati-agent@yourtenant.onmicrosoft.com.

.PARAMETER UseCloudMemory
  Provision Azure Blob Storage for operational data (default: local).

.PARAMETER WithStorageAccount
  Use the named Azure Storage Account instead of the deterministic
  per-tenant default. Created if missing. Mutually exclusive with
  -CreateNewStorage. Only meaningful with -UseCloudMemory.

.PARAMETER WithContainer
  Use the named blob container instead of the agent-<oid> default.
  Only meaningful with -UseCloudMemory.

.PARAMETER CreateNewStorage
  Force creation of a fresh randomly-suffixed Storage Account even when
  the deterministic-name one already exists. Mutually exclusive with
  -WithStorageAccount. Only meaningful with -UseCloudMemory.

.PARAMETER ConfigureA365WorkIq
  Run the interactive Microsoft Agent 365 Work IQ Word developer setup:
  a365 develop add-mcp-servers mcp_WordServer, a365 setup permissions mcp
  against the existing Entrabot Blueprint, then validate ToolingManifest.json.

.PARAMETER A365AgentName
  Deprecated compatibility parameter. Work IQ setup now uses the existing
  Entrabot Blueprint ID from .entrabot-state.json.

.PARAMETER Status
    Skip setup and run the consolidated status command via status-windows.ps1.

.PARAMETER Json
    With -Status, output machine-readable JSON.

.PARAMETER HealthOnly
    With -Status, only print health checks.

.PARAMETER Strict
    With -Status, return non-zero when health checks fail.

.EXAMPLE
  .\scripts\setup-windows.ps1 -NewChain -UpnSuffix winagent

.EXAMPLE
  .\scripts\setup-windows.ps1 -NewChain -UpnSuffix winagent -UseCloudMemory `
      -WithStorageAccount mycorpstg -WithContainer winagent-mem
#>

[CmdletBinding()]
param(
    [switch]$NewChain,
    [string]$UseBlueprint = "",
    [string]$UpnSuffix = "",
    [string]$AgentUserUpn = "",
    [switch]$UseCloudMemory,
    [string]$WithStorageAccount = "",
    [string]$WithContainer = "",
    [switch]$CreateNewStorage,
    [switch]$ConfigureA365WorkIq,
    [string]$A365AgentName = "EntraBot Code Agent",
    [switch]$Migrate,
    [switch]$Status,
    [switch]$Json,
    [switch]$HealthOnly,
    [switch]$Strict,
    [switch]$Help
)

# Detect the "blue Windows PowerShell 5.1 vs black PowerShell 7" trap before
# strict mode trips on PS-7-only automatic variables ($IsWindows, $IsLinux).
# Done inline (not via #Requires) so the message can be actionable.
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Host ""
    Write-Host "ERROR: This script needs PowerShell 7+, but you launched it from" -ForegroundColor Red
    Write-Host "       Windows PowerShell $($PSVersionTable.PSVersion)." -ForegroundColor Red
    Write-Host ""
    Write-Host "Two different products on Windows — easy to confuse:" -ForegroundColor Yellow
    Write-Host "  - 'Windows PowerShell' (blue icon, pre-installed) is 5.1, NOT this." -ForegroundColor Yellow
    Write-Host "  - 'PowerShell' / pwsh (black icon, separate install) is 7+, USE THIS." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "How to fix:" -ForegroundColor Cyan
    Write-Host "  Close this window. Open Start, type 'pwsh' (NOT 'PowerShell'),"
    Write-Host "  pick the result with the BLACK icon, and re-run the same command."
    Write-Host ""
    Write-Host "  If pwsh is missing, run scripts\prereqs-windows.ps1 first to install it."
    Write-Host ""
    exit 1
}

# Mutex: -CreateNewStorage and -WithStorageAccount both pin the storage
# account name; only one can win.
if ($CreateNewStorage -and $WithStorageAccount) {
    Write-Host "ERROR: -CreateNewStorage and -WithStorageAccount are mutually exclusive." -ForegroundColor Red
    exit 2
}

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

if ($Status) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
    & (Join-Path $ProjectRoot 'status-windows.ps1') -Json:$Json -HealthOnly:$HealthOnly -Strict:$Strict
    exit $LASTEXITCODE
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
    Write-Host "═══ Step $n / 9 — $msg" -ForegroundColor Cyan
}
function Success($msg) { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Fail($msg)    { Write-Host "  ✗ $msg" -ForegroundColor Red; exit 1 }

# Idempotent .env writer: upsert each key (replace in place, append if absent) and de-duplicate
# ANY repeated keys (keeping the first), so re-runs never accumulate stale duplicates the way
# Out-File/Add-Content -Append did. A $null value removes the key (e.g. switching memory modes).
# Comments and blank lines are preserved.
function Update-EnvFile {
    param([string]$Path, [hashtable]$Values)
    $existing = if (Test-Path $Path) { @(Get-Content -Path $Path) } else { @() }
    $seen = @{}
    $out = foreach ($line in $existing) {
        $m = [regex]::Match($line, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
        if (-not $m.Success) { $line; continue }   # comments / blanks pass through
        $key = $m.Groups[1].Value
        if ($seen.ContainsKey($key)) { continue }   # drop any duplicate of an already-emitted key
        $seen[$key] = $true
        if ($Values.ContainsKey($key)) {
            if ($null -ne $Values[$key]) { "$key=$($Values[$key])" }  # replace (or drop if null)
        } else {
            $line                                    # keep unrelated keys as-is
        }
    }
    foreach ($k in $Values.Keys) {                   # append keys not already in the file
        if (-not $seen.ContainsKey($k) -and $null -ne $Values[$k]) { $out += "$k=$($Values[$k])" }
    }
    Set-Content -Path $Path -Value $out -Encoding utf8
}
function Ensure-A365ToolingManifest {
    $manifestPath = Join-Path $ProjectRoot 'ToolingManifest.json'
    if (-not (Test-Path $manifestPath)) {
        Set-Content -Path $manifestPath -Value '{"mcpServers":[]}' -Encoding utf8
        Success "Created minimal ToolingManifest.json for A365 Work IQ"
    }
}

function Write-A365Config {
    param(
        [string]$TenantId,
        [string]$BlueprintAppId,
        [string]$BlueprintObjectId,
        [string]$AgentId
    )

    $clientAppId = az ad app list --display-name "Agent 365 CLI" --query "[0].appId" -o tsv 2>$null
    if (-not $clientAppId) {
        Fail "Agent 365 CLI app was not found. Re-run and choose C during 'a365 setup requirements', or create the app before configuring Work IQ."
    }
    if (-not $BlueprintAppId) {
        Fail "Blueprint ID not found. Entrabot provisioning must complete before configuring A365 Work IQ."
    }
    $AgentIdentityDisplayName = az ad sp show --id $AgentId --query displayName -o tsv 2>$null
    if (-not $AgentIdentityDisplayName) {
        Fail "Agent Identity display name not found for $AgentId. Entrabot provisioning must complete before configuring A365 Work IQ."
    }

    $configPath = Join-Path $ProjectRoot 'a365.config.json'
    $config = @{}
    if (Test-Path $configPath) {
        $existing = Get-Content $configPath -Raw | ConvertFrom-Json
        foreach ($property in $existing.PSObject.Properties) {
            $config[$property.Name] = $property.Value
        }
    }

    $config["tenantId"] = $TenantId
    $config["clientAppId"] = $clientAppId
    $config["agentBlueprintId"] = $BlueprintAppId
    $config["agentBlueprintDisplayName"] = "EntraBot Code Agent"
    $config["agentIdentityDisplayName"] = $AgentIdentityDisplayName
    $config["deploymentProjectPath"] = $ProjectRoot
    if ($BlueprintObjectId) { $config["agentBlueprintObjectId"] = $BlueprintObjectId }
    if ($AgentId) { $config["agentIdentityId"] = $AgentId }

    $config | ConvertTo-Json -Depth 8 | Set-Content -Path $configPath -Encoding utf8
    Success "A365 config points to existing Entrabot Blueprint"
}

function Configure-A365WorkIq {
    param(
        [string]$TenantId,
        [string]$BlueprintAppId,
        [string]$BlueprintObjectId,
        [string]$AgentId
    )
    $A365WorkIqMcpServers = @("mcp_WordServer", "mcp_ODSPRemoteServer")

    Write-Host ""
    Write-Host "Configuring Microsoft Agent 365 Work IQ Word + OneDrive/SharePoint..." -ForegroundColor Cyan
    Write-Host "This may open an interactive Microsoft sign-in/device-code flow." -ForegroundColor Yellow
    Write-Host "If the Agent 365 CLI app is missing, choose C when prompted to create it." -ForegroundColor Yellow

    Ensure-A365ToolingManifest

    Write-A365Config -TenantId $TenantId -BlueprintAppId $BlueprintAppId -BlueprintObjectId $BlueprintObjectId -AgentId $AgentId

    a365 setup requirements
    if ($LASTEXITCODE -ne 0) { Fail "a365 setup requirements failed" }

    a365 develop add-mcp-servers $A365WorkIqMcpServers --project-path $ProjectRoot
    if ($LASTEXITCODE -ne 0) { Fail "a365 develop add-mcp-servers failed" }

    & $VenvPython (Join-Path $ScriptDir 'ensure_a365_work_iq_permissions.py') '--blueprint-app-id', $BlueprintAppId
    if ($LASTEXITCODE -ne 0) { Fail "ensure_a365_work_iq_permissions.py failed" }

    $permissionsOutput = a365 setup permissions mcp 2>&1
    $permissionsExit = $LASTEXITCODE
    $permissionsOutput | ForEach-Object { Write-Host $_ }
    if ($permissionsExit -ne 0) { Fail "a365 setup permissions mcp failed" }
    if (($permissionsOutput -join "`n") -match "OAuth2 grants failed") {
        Fail "a365 setup permissions mcp reported OAuth2 grants failed; Work IQ permissions are incomplete. Resolve the Agent 365 Tools service principal/admin-consent issue, then rerun setup-windows.ps1."
    }

    & python (Join-Path $ScriptDir 'spike_a365_work_iq.py')
    if ($LASTEXITCODE -ne 0) { Fail "A365 Work IQ manifest validation failed" }
    Success "A365 Work IQ Word manifest configured"
}

# ═══════════════════════════════════════════════════════════════════════════
# 3. Probe prereqs
# ═══════════════════════════════════════════════════════════════════════════
Step 1 "Probing prerequisites"

$missing = @()
foreach ($tool in 'python', 'az', 'git', 'pwsh', 'a365') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        $missing += $tool
    }
}
if ($missing) {
    Fail "Missing tools: $($missing -join ', '). Run scripts\prereqs-windows.ps1 and retry."
}
Success "Found: python, az, git, pwsh, a365"

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
from entrabot.config import migrate_legacy_data_dir
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
if ($AgentUserUpn) {
    $env:ENTRABOT_AGENT_USER_UPN = $AgentUserUpn
} elseif ($UseBlueprint -and $UpnSuffix) {
    $env:_ENTRABOT_UPN_SUFFIX = $UpnSuffix
}
if ($ConfigureA365WorkIq) {
    $env:ENTRABOT_ASSIGN_WORK_IQ_LICENSE = '1'
}

# entra_provisioning.py + create_entra_agent_ids.py both read az CLI
# session state directly, identical to setup.sh.
& $VenvPython (Join-Path $ScriptDir 'entra_provisioning.py')
if ($LASTEXITCODE -ne 0) { Fail "entra_provisioning.py failed" }

& $VenvPython (Join-Path $ScriptDir 'create_entra_agent_ids.py') @args
if ($LASTEXITCODE -ne 0) { Fail "create_entra_agent_ids.py failed" }

# Read back IDs from .entrabot-state.json — needed for cloud-memory step
$statePath = Join-Path $ProjectRoot '.entrabot-state.json'
$BlueprintAppId = ""
$BlueprintObjectId = ""
$AgentId = ""
$AgentUserId = ""
if (Test-Path $statePath) {
    $state = Get-Content $statePath -Raw | ConvertFrom-Json
    $BlueprintAppId = if ($state.PSObject.Properties['BLUEPRINT_APP_ID']) { $state.BLUEPRINT_APP_ID } else { "" }
    $BlueprintObjectId = if ($state.PSObject.Properties['BLUEPRINT_OBJECT_ID']) { $state.BLUEPRINT_OBJECT_ID } else { "" }
    $AgentId = if ($state.PSObject.Properties['AGENT_ID']) { $state.AGENT_ID } else { "" }
    $AgentUserId = if ($state.PSObject.Properties['AGENT_USER_ID']) { $state.AGENT_USER_ID } else { "" }
}

if ($ConfigureA365WorkIq) {
    Configure-A365WorkIq -TenantId $account.tenantId -BlueprintAppId $BlueprintAppId -BlueprintObjectId $BlueprintObjectId -AgentId $AgentId
}

# ═══════════════════════════════════════════════════════════════════════════
# 7. Generate Blueprint cert (TPM-first / software-fallback)
# ═══════════════════════════════════════════════════════════════════════════
Step 6 "Generating Blueprint cert (TPM-first / software-fallback)"

$derPath = Join-Path $env:TEMP "entrabot-blueprint-$(Get-Random).cer"
$certOutput = & $VenvPython (Join-Path $ScriptDir 'generate_windows_cert.py') `
    --subject "CN=entrabot-blueprint" `
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
Update-EnvFile $envPath @{
    ENTRABOT_TENANT_ID                 = $account.tenantId
    ENTRABOT_BLUEPRINT_CERT_THUMBPRINT = $x5tS256
    ENTRABOT_BLUEPRINT_CERT_SHA1       = $thumbprint
    ENTRABOT_BLUEPRINT_KSP             = $ksp
}

# icacls :M (modify) — NOT :R (read-only). :R would self-brick: setup
# re-runs and rotation both need to update .env.
$user = "$env:USERDOMAIN\$env:USERNAME"
icacls $envPath /inheritance:r /grant:r "${user}:M" | Out-Null
Success ".env locked to $user (modify, per D10)"

# ═══════════════════════════════════════════════════════════════════════════
# 8. Cloud memory — Azure Blob Storage provisioning (ADR-005, Phase 5)
# ═══════════════════════════════════════════════════════════════════════════
Step 8 "Cloud memory (Azure Blob Storage)"

if (-not $UseCloudMemory) {
    # local memory: set the flag, clear any stale cloud keys (idempotent across mode switches)
    Update-EnvFile $envPath @{
        ENTRABOT_KEEP_MEMORY_LOCAL = 'true'; ENTRABOT_BLOB_ENDPOINT = $null; ENTRABOT_BLOB_CONTAINER = $null
    }
    Success "Memory mode: LOCAL (pass -UseCloudMemory to opt in)"
} elseif (-not $AgentUserId) {
    Write-Host "  ⚠ Skipping blob storage — no Agent User ID found in state" -ForegroundColor Yellow
    Update-EnvFile $envPath @{ ENTRABOT_KEEP_MEMORY_LOCAL = 'true' }
} else {
    $provArgs = @(
        '--tenant-id', $account.tenantId,
        '--agent-user-object-id', $AgentUserId
    )
    if ($WithStorageAccount) { $provArgs += @('--with-storage-account', $WithStorageAccount) }
    if ($WithContainer)      { $provArgs += @('--with-container', $WithContainer) }
    if ($CreateNewStorage)   { $provArgs += '--create-new-storage' }

    # Provisioner prints progress on stderr and KEY=VALUE lines on stdout.
    # PS 5.1/7 native-stderr handling: capture stdout into a variable, let
    # stderr stream to the console so the user sees az progress.
    $provStdout = & $VenvPython (Join-Path $ScriptDir 'provision_blob_storage.py') @provArgs
    $provRc = $LASTEXITCODE

    if ($provRc -ne 0) {
        Write-Host "  ⚠ Blob storage provisioning failed — falling back to local-only memory" -ForegroundColor Yellow
        Update-EnvFile $envPath @{ ENTRABOT_KEEP_MEMORY_LOCAL = 'true' }
    } else {
        $blobEndpoint  = ($provStdout | Select-String '^BLOB_ENDPOINT=(.+)$').Matches[0].Groups[1].Value
        $blobContainer = ($provStdout | Select-String '^BLOB_CONTAINER=(.+)$').Matches[0].Groups[1].Value
        if (-not $blobEndpoint -or -not $blobContainer) {
            Write-Host "  ⚠ Provisioner returned no endpoint/container — using local-only memory" -ForegroundColor Yellow
            Update-EnvFile $envPath @{ ENTRABOT_KEEP_MEMORY_LOCAL = 'true' }
        } else {
            # cloud memory: set blob keys, clear the local-only flag
            Update-EnvFile $envPath @{
                ENTRABOT_BLOB_ENDPOINT = $blobEndpoint; ENTRABOT_BLOB_CONTAINER = $blobContainer
                ENTRABOT_KEEP_MEMORY_LOCAL = $null
            }
            Success "Blob storage ready: $blobEndpoint/$blobContainer"
        }
    }
}

# ═══════════════════════════════════════════════════════════════════════════
# 9. Register MCP server via mcp_config.py
# ═══════════════════════════════════════════════════════════════════════════
Step 9 "Registering MCP server"

$mcpBinary = Join-Path $ProjectRoot '.venv\Scripts\entrabot-mcp.exe'
if (-not (Test-Path $mcpBinary)) { Fail "MCP binary not found at $mcpBinary" }
& $VenvPython (Join-Path $ScriptDir 'mcp_config.py') --binary $mcpBinary --project-root $ProjectRoot
if ($LASTEXITCODE -ne 0) { Fail "mcp_config.py failed" }
Success "MCP server registered for Claude Code + Copilot CLI"

Write-Host ""
Write-Host "═══ Setup complete ═══" -ForegroundColor Green
Write-Host "  KSP:        $ksp"
Write-Host "  Thumbprint: $thumbprint"
Write-Host "  Run: pwsh -File scripts\deploy-windows.ps1 to rotate cert."
