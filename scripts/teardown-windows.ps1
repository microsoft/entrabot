<#
.SYNOPSIS
  EntraBot — Windows teardown. Reverse of setup-windows.ps1.

.DESCRIPTION
  Removes:
    - Blueprint cert(s) from Cert:\CurrentUser\My matching the
      Subject CN=entrabot-blueprint.
    - %LOCALAPPDATA%\entrabot\ data dir.
    - .env BLUEPRINT_CERT_* lines (preserves the rest of the file).
    - MSAL cache.
    - MCP registration entries from .mcp.json and Copilot's mcp-config.json.

  Does NOT delete the Entra app registrations (Blueprint / Agent
  Identity / Agent User) — those persist in the tenant. Use the
  cleanup-orphans.sh equivalent in the cloud admin portal.
#>

[CmdletBinding()]
param([switch]$Force, [switch]$Help)

# Detect the "blue Windows PowerShell 5.1 vs black PowerShell 7" trap before
# strict mode trips on PS-7-only automatic variables ($IsWindows, $IsLinux).
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

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Help) { Get-Help $PSCommandPath -Detailed; exit 0 }
if (-not $IsWindows) { throw "teardown-windows.ps1 must run on Windows." }

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

if (-not $Force) {
    $resp = Read-Host "Remove all local entrabot state on this machine? [y/N]"
    if ($resp -notmatch '^[Yy]') { Write-Host "aborted."; exit 0 }
}

# Remove certs by Subject — safer than thumbprint (which may be stale).
Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.Subject -eq 'CN=entrabot-blueprint' } |
    ForEach-Object {
        Write-Host "Removing cert $($_.Thumbprint)..."
        Remove-Item $_.PSPath -Force
    }

# Local data dir.
$dataDir = Join-Path $env:LOCALAPPDATA 'entrabot'
if (Test-Path $dataDir) {
    Write-Host "Removing $dataDir..."
    Remove-Item -Recurse -Force $dataDir
}

# .env: strip BLUEPRINT_CERT_* lines but keep the rest.
$envPath = Join-Path $ProjectRoot '.env'
if (Test-Path $envPath) {
    $kept = Get-Content $envPath | Where-Object {
        $_ -notmatch '^(ENTRABOT_BLUEPRINT_CERT_THUMBPRINT|ENTRABOT_BLUEPRINT_CERT_SHA1|ENTRABOT_BLUEPRINT_KSP)='
    }
    $kept | Set-Content $envPath -Encoding utf8
    Write-Host "Stripped BLUEPRINT_CERT_* from .env."
}

# Remove entrabot MCP entries (no unregister CLI; edit JSON directly).
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$pyExe = if (Test-Path $VenvPython) { $VenvPython } else { 'python' }
$claudeMcp = Join-Path $ProjectRoot '.mcp.json'
$copilotMcp = if ($env:COPILOT_HOME) { Join-Path $env:COPILOT_HOME 'mcp-config.json' } `
              else { Join-Path $env:USERPROFILE '.copilot\mcp-config.json' }
foreach ($target in @($claudeMcp, $copilotMcp)) {
    if (Test-Path $target) {
        & $pyExe -c "import json,sys,pathlib; p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text() or '{}'); d.get('mcpServers',{}).pop('entrabot',None); p.write_text(json.dumps(d, indent=2))" $target
    }
}

Write-Host "Teardown complete." -ForegroundColor Green
