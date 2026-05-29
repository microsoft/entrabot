<#
.SYNOPSIS
  EntraBot status wrapper for Windows.

.DESCRIPTION
  Ensures the local Python environment exists, installs the EntraBot
  provisioning dependencies if needed, loads .env, and delegates to the
  consolidated scripts/show_agent_status.py command.

.PARAMETER Json
  Output machine-readable JSON.

.PARAMETER HealthOnly
  Only print health checks.

.PARAMETER Strict
  Return non-zero when health checks fail.

.PARAMETER Help
  Show the underlying status command help.

.EXAMPLE
  .\status-windows.ps1

.EXAMPLE
  .\status-windows.ps1 -HealthOnly

.EXAMPLE
  .\status-windows.ps1 -Json
#>

[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$HealthOnly,
    [switch]$Strict,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSCommandPath
$WindowsVenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$UnixVenvPython = Join-Path $ProjectRoot '.venv/bin/python'
$VenvPython = if (Test-Path $WindowsVenvPython) { $WindowsVenvPython } elseif (Test-Path $UnixVenvPython) { $UnixVenvPython } else { $WindowsVenvPython }

function Find-Python312 {
    foreach ($candidate in @('python3.12', 'python3.13', 'python')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        & $cmd.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return $cmd.Source }
    }
    return $null
}

if (-not (Test-Path $VenvPython)) {
    $Python = Find-Python312
    if (-not $Python) {
        Write-Host "ERROR: Python 3.12+ is required to run EntraBot status." -ForegroundColor Red
        exit 1
    }
    Write-Host "Creating local Python environment at .venv..." -ForegroundColor Yellow
    & $Python -m venv (Join-Path $ProjectRoot '.venv')
}

& $VenvPython -c "import azure.identity, entrabot" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing EntraBot status dependencies into .venv..." -ForegroundColor Yellow
    & $VenvPython -m pip install -e '.[provisioning]'
}

$EnvPath = Join-Path $ProjectRoot '.env'
if (Test-Path $EnvPath) {
    foreach ($line in Get-Content $EnvPath) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $parts = $line -split '=', 2
        if ($parts[0]) {
            [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process')
        }
    }
}

$ForwardArgs = @()
if ($Json) { $ForwardArgs += '--json' }
if ($HealthOnly) { $ForwardArgs += '--health-only' }
if ($Strict) { $ForwardArgs += '--strict' }
if ($Help) { $ForwardArgs += '--help' }

$StatusScript = Join-Path (Join-Path $ProjectRoot 'scripts') 'show_agent_status.py'
& $VenvPython $StatusScript @ForwardArgs
exit $LASTEXITCODE