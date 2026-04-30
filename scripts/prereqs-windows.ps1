# requires -Version 5.1
<#
.SYNOPSIS
  EntraClaw — Windows prerequisite installer.

.DESCRIPTION
  Checks for and installs all prerequisites needed to run setup-windows.ps1:
    1. PowerShell 7+ (winget install Microsoft.PowerShell)
    2. Python 3.12+ (winget install Python.Python.3.12)
    3. Git (winget install Git.Git)
    4. Azure CLI (winget install Microsoft.AzureCLI)
    5. Visual Studio Build Tools with C++ workload (needed for native Python
       packages like cffi/cryptography that compile C extensions)
    6. Windows SDK (included with VS Build Tools C++ workload)

  Run this BEFORE setup-windows.ps1. It's safe to re-run — skips anything
  already installed.

  This script can run from Windows PowerShell 5.1 (the one that ships with
  Windows) so you don't need pwsh already installed.

.EXAMPLE
  .\scripts\prereqs-windows.ps1
#>

[CmdletBinding()]
param(
    [switch]$SkipBuildTools,
    [switch]$Help
)

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

$ErrorActionPreference = 'Stop'

# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

function Write-Step($msg) {
    Write-Host ""
    Write-Host "══ $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "  ○ $msg" -ForegroundColor DarkGray }
function Write-Install($msg) { Write-Host "  → $msg" -ForegroundColor Yellow }
function Write-Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "  ✗ $msg" -ForegroundColor Red }

function Test-CommandExists($cmd) {
    $null -ne (Get-Command $cmd -ErrorAction SilentlyContinue)
}

function Refresh-PathEnv {
    # Reload PATH from registry so newly installed tools are visible
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

$installed = @()
$alreadyPresent = @()
$failed = @()

# ═══════════════════════════════════════════════════════════════════════════
# 0. Check winget is available
# ═══════════════════════════════════════════════════════════════════════════
Write-Step "Checking winget (Windows Package Manager)"

if (-not (Test-CommandExists 'winget')) {
    Write-Err "winget not found. It ships with Windows 11 and Windows 10 (1809+)."
    Write-Err "Install from: https://aka.ms/getwinget"
    exit 1
}
Write-Ok "winget available"

# ═══════════════════════════════════════════════════════════════════════════
# 1. PowerShell 7+
# ═══════════════════════════════════════════════════════════════════════════
Write-Step "PowerShell 7+"

$pwshPath = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwshPath) {
    $pwshVer = & pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'
    if ([version]$pwshVer -ge [version]'7.0') {
        Write-Ok "PowerShell $pwshVer already installed"
        $alreadyPresent += "PowerShell 7"
    } else {
        Write-Install "Upgrading PowerShell (found $pwshVer, need 7+)..."
        winget install --id Microsoft.PowerShell --source winget --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) { $installed += "PowerShell 7" } else { $failed += "PowerShell 7" }
    }
} else {
    Write-Install "Installing PowerShell 7..."
    winget install --id Microsoft.PowerShell --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -eq 0) { $installed += "PowerShell 7" } else { $failed += "PowerShell 7" }
}

# ═══════════════════════════════════════════════════════════════════════════
# 2. Python 3.12+
# ═══════════════════════════════════════════════════════════════════════════
Write-Step "Python 3.12+"

$pythonOk = $false
if (Test-CommandExists 'python') {
    $pyVer = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    if ($pyVer -and [version]$pyVer -ge [version]'3.12') {
        Write-Ok "Python $pyVer already installed"
        $alreadyPresent += "Python $pyVer"
        $pythonOk = $true

        # Check it's not the Microsoft Store stub
        $pyPrefix = & python -c "import sys; print(sys.base_prefix)" 2>$null
        if ($pyPrefix -and $pyPrefix -match 'WindowsApps') {
            Write-Warn "This is the Microsoft Store Python stub — it may not work correctly."
            Write-Warn "Consider installing the full Python from python.org or via winget."
            $pythonOk = $false
        }
    }
}

if (-not $pythonOk) {
    Write-Install "Installing Python 3.12..."
    winget install --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements
    Refresh-PathEnv
    if ($LASTEXITCODE -eq 0) { $installed += "Python 3.12" } else { $failed += "Python 3.12" }
}

# ═══════════════════════════════════════════════════════════════════════════
# 3. Git
# ═══════════════════════════════════════════════════════════════════════════
Write-Step "Git"

if (Test-CommandExists 'git') {
    $gitVer = (& git --version) -replace 'git version ',''
    Write-Ok "Git $gitVer already installed"
    $alreadyPresent += "Git"
} else {
    Write-Install "Installing Git..."
    winget install --id Git.Git --source winget --accept-package-agreements --accept-source-agreements
    Refresh-PathEnv
    if ($LASTEXITCODE -eq 0) { $installed += "Git" } else { $failed += "Git" }
}

# ═══════════════════════════════════════════════════════════════════════════
# 4. Azure CLI
# ═══════════════════════════════════════════════════════════════════════════
Write-Step "Azure CLI"

if (Test-CommandExists 'az') {
    $azVer = (& az version --output tsv 2>$null | Select-Object -First 1)
    Write-Ok "Azure CLI already installed ($azVer)"
    $alreadyPresent += "Azure CLI"
} else {
    Write-Install "Installing Azure CLI..."
    winget install --id Microsoft.AzureCLI --source winget --accept-package-agreements --accept-source-agreements
    Refresh-PathEnv
    if ($LASTEXITCODE -eq 0) { $installed += "Azure CLI" } else { $failed += "Azure CLI" }
}

# ═══════════════════════════════════════════════════════════════════════════
# 5. Visual Studio Build Tools + C++ workload (for native Python packages)
# ═══════════════════════════════════════════════════════════════════════════
Write-Step "Visual Studio Build Tools (C++ workload for native Python packages)"

if ($SkipBuildTools) {
    Write-Skip "Skipped (-SkipBuildTools flag)"
} else {
    # Check if cl.exe is available (indicates C++ build tools are installed)
    $vsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $hasBuildTools = $false

    if (Test-Path $vsWhere) {
        $vsInstalls = & $vsWhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
        if ($vsInstalls) {
            $hasBuildTools = $true
        }
    }

    if ($hasBuildTools) {
        Write-Ok "Visual Studio Build Tools (C++ workload) already installed"
        $alreadyPresent += "VS Build Tools"
    } else {
        Write-Install "Installing Visual Studio Build Tools with C++ workload..."
        Write-Install "This may take 5-10 minutes and requires ~6 GB of disk space."
        Write-Host ""

        # Install VS Build Tools with the C++ desktop workload
        # This includes: MSVC compiler, Windows SDK, CMake, C++ core features
        winget install --id Microsoft.VisualStudio.2022.BuildTools --source winget `
            --accept-package-agreements --accept-source-agreements `
            --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"

        if ($LASTEXITCODE -eq 0) {
            $installed += "VS Build Tools"
        } else {
            # winget may report non-zero even on success for VS installs
            # Check again with vswhere
            Start-Sleep -Seconds 5
            if (Test-Path $vsWhere) {
                $vsInstalls = & $vsWhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
                if ($vsInstalls) {
                    Write-Ok "Visual Studio Build Tools installed (winget exit code was non-zero but install succeeded)"
                    $installed += "VS Build Tools"
                } else {
                    $failed += "VS Build Tools"
                }
            } else {
                $failed += "VS Build Tools"
            }
        }
    }
}

# ═══════════════════════════════════════════════════════════════════════════
# 6. Refresh PATH and final validation
# ═══════════════════════════════════════════════════════════════════════════
Write-Step "Final validation"

Refresh-PathEnv

$allGood = $true
$checks = @(
    @{ Name = "pwsh";   Cmd = "pwsh";   MinVer = $null },
    @{ Name = "python"; Cmd = "python"; MinVer = "3.12" },
    @{ Name = "git";    Cmd = "git";    MinVer = $null },
    @{ Name = "az";     Cmd = "az";     MinVer = $null }
)

foreach ($check in $checks) {
    if (Test-CommandExists $check.Cmd) {
        Write-Ok "$($check.Name) ✓"
    } else {
        Write-Err "$($check.Name) NOT FOUND after install — you may need to restart your terminal"
        $allGood = $false
    }
}

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  PREREQUISITE CHECK COMPLETE" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

if ($alreadyPresent) {
    Write-Host "  Already installed:" -ForegroundColor Green
    foreach ($item in $alreadyPresent) { Write-Host "    • $item" -ForegroundColor Green }
}
if ($installed) {
    Write-Host "  Newly installed:" -ForegroundColor Yellow
    foreach ($item in $installed) { Write-Host "    • $item" -ForegroundColor Yellow }
}
if ($failed) {
    Write-Host "  FAILED to install:" -ForegroundColor Red
    foreach ($item in $failed) { Write-Host "    • $item" -ForegroundColor Red }
}

Write-Host ""
if ($failed) {
    Write-Err "Some prerequisites failed to install. Fix them manually and re-run."
    exit 1
} elseif (-not $allGood) {
    Write-Warn "Installs succeeded but some tools aren't on PATH yet."
    Write-Warn "Close this terminal, open a NEW one, and run:"
    Write-Warn "  .\scripts\setup-windows.ps1"
    exit 0
} else {
    Write-Ok "All prerequisites ready!"
    Write-Host ""
    Write-Host "  Next step:" -ForegroundColor White
    Write-Host "    .\scripts\setup-windows.ps1 -NewChain -UpnSuffix <yourname>" -ForegroundColor White
    Write-Host ""
    exit 0
}
