<#
.SYNOPSIS
  Entrabot - show the agent's status (Windows).

.DESCRIPTION
  Shows the agent's identity, permissions, licences and health checks. Changes nothing
  and installs nothing.

  This script is a thin wrapper. The work is done by scripts\show_agent_status.py, run
  through:
      python -m entrabot.winsetup status
  which starts it with the project's own Python environment and the same Azure CLI that
  setup used. It runs on the Windows PowerShell 5.1 that ships with Windows, and on
  PowerShell 7. The exit code is the status command's.

  If Entrabot is not installed in this folder yet, run scripts\setup-windows.cmd first.

.PARAMETER Json
  Output machine-readable JSON.

.PARAMETER HealthOnly
  Only print health checks.

.PARAMETER Strict
  Return non-zero when health checks fail.

.PARAMETER FullThumbprints
  Show certificate thumbprints in full, to compare them with the Microsoft Entra
  admin center. By default only their last four characters are shown.

.PARAMETER Help
  Show this help.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File status-windows.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File status-windows.ps1 -HealthOnly -Strict

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File status-windows.ps1 -Json
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Json,
    [switch]$HealthOnly,
    [switch]$Strict,
    [switch]$FullThumbprints,
    [switch]$Help
)

# Windows PowerShell 5.1 writes progress records as CLIXML to stderr when its output is
# not a console. Nothing here needs a progress bar.
$ProgressPreference = 'SilentlyContinue'

# Windows PowerShell started from a PowerShell 7 terminal through anything other than
# PowerShell 7 itself (cmd.exe, Python, ...) keeps PowerShell 7's module paths, resolves
# its in-box modules (Microsoft.PowerShell.Utility, ...) to versions it cannot load, and
# then cannot find Write-Host or Get-FileHash (about_PSModulePath, "Starting Windows
# PowerShell from PowerShell 7"). So under Windows PowerShell, put back its own default
# locations for this process, before the first cmdlet that needs a module. No cmdlet is
# used here: even Join-Path lives in a module. PowerShell 7 builds its own; nothing to do.
if ($PSVersionTable.PSVersion.Major -lt 6) {
    $windowsPowerShellModulePaths = @()
    $documentsFolder = [Environment]::GetFolderPath('MyDocuments')
    if ($documentsFolder) {
        $windowsPowerShellModulePaths += [System.IO.Path]::Combine($documentsFolder, 'WindowsPowerShell\Modules')
    }
    $programFilesFolder = [Environment]::GetFolderPath('ProgramFiles')
    if ($programFilesFolder) {
        $windowsPowerShellModulePaths += [System.IO.Path]::Combine($programFilesFolder, 'WindowsPowerShell\Modules')
    }
    $windowsPowerShellModulePaths += [System.IO.Path]::Combine($PSHOME, 'Modules')
    $env:PSModulePath = $windowsPowerShellModulePaths -join ';'
}

# =============================================================================
# Pure helpers, loadable by the tests (dot-source this file). This file must stay
# ASCII-only: Windows PowerShell 5.1 reads a file without a byte-order mark in the
# ANSI code page, where one non-ASCII character is a parse error.
# =============================================================================

function Get-StatusArguments {
    # The argument list handed to `python -m entrabot.winsetup`. Values are separate
    # array elements, so nothing is ever quoted by hand.
    param([string]$ProjectRoot, [bool]$Json, [bool]$HealthOnly, [bool]$Strict, [bool]$FullThumbprints)
    $list = @('status', '--project-root', $ProjectRoot)
    if ($Json) { $list += '--json' }
    if ($HealthOnly) { $list += '--health-only' }
    if ($Strict) { $list += '--strict' }
    if ($FullThumbprints) { $list += '--full-thumbprints' }
    return $list
}

if ($MyInvocation.InvocationName -eq '.' -or $env:ENTRABOT_SETUP_FUNCTIONS_ONLY -eq '1') { return }

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Strict mode makes reading $LASTEXITCODE an error until some native command has set it.
$global:LASTEXITCODE = 0

if ($env:OS -ne 'Windows_NT') {
    # Written to the error stream so a -Json caller never mistakes it for output.
    [Console]::Error.WriteLine('ERROR: status-windows.ps1 must run on Windows. On macOS or Linux run ./status.sh.')
    exit 1
}

$ProjectRoot = Split-Path -Parent $PSCommandPath
$venvPython = Join-Path (Join-Path $ProjectRoot '.venv') 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    [Console]::Error.WriteLine('ERROR: Entrabot is not installed in this folder yet. Run scripts\setup-windows.cmd first.')
    exit 1
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$handoff = @('-m', 'entrabot.winsetup') + @(Get-StatusArguments -ProjectRoot $ProjectRoot -Json ([bool]$Json) -HealthOnly ([bool]$HealthOnly) -Strict ([bool]$Strict) -FullThumbprints ([bool]$FullThumbprints))
& $venvPython @handoff
exit $LASTEXITCODE
