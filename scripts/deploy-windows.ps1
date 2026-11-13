<#
.SYNOPSIS
  Entrabot - rotate this computer's Blueprint certificate (Windows).

.DESCRIPTION
  Replaces the certificate this computer uses to sign the agent in. The certificate in
  use stays in use until the new one is proven to work:

    1. A new certificate is created on this computer (in the TPM where there is one).
    2. It is added to the agent's Blueprint, beside the current one.
    3. The agent signs in with it, waiting for Microsoft Entra to take it up.
    4. This computer's settings (.env) switch to the new certificate.
    5. The old certificate is removed from the Blueprint and deleted from this computer,
       together with its private key.

  If steps 1-3 fail, the new certificate is undone and nothing else changes. If step 5
  fails, the agent is already working on the new certificate; run this again to finish.

  This script is a thin wrapper. The work is done by:
      python -m entrabot.winsetup rotate-cert
  It runs on the Windows PowerShell 5.1 that ships with Windows, and on PowerShell 7.
  Afterwards, restart your assistant (Claude Code or GitHub Copilot CLI): the agent reads
  its certificate when it starts.

  Exit codes: 0 rotated, 1 failed (the current certificate is still in use), 3 rotated but
  cleanup is pending (run it again), 130 cancelled.

.PARAMETER Yes
  Do not ask before rotating.

.PARAMETER Status
  Skip rotation and show the agent's status instead (status-windows.ps1).

.PARAMETER Json
  With -Status, output machine-readable JSON.

.PARAMETER HealthOnly
  With -Status, only print health checks.

.PARAMETER Strict
  With -Status, return non-zero when health checks fail.

.PARAMETER Help
  Show this help.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy-windows.ps1 -Yes -Verbose
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Yes,
    [switch]$Status,
    [switch]$Json,
    [switch]$HealthOnly,
    [switch]$Strict,
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

function Get-RotateArguments {
    # The argument list handed to `python -m entrabot.winsetup`. Values are separate
    # array elements, so nothing is ever quoted by hand.
    param([string]$ProjectRoot, [bool]$AssumeYes, [bool]$ShowEverything)
    $list = @('rotate-cert', '--project-root', $ProjectRoot)
    if ($AssumeYes) { $list += '--yes' }
    if ($ShowEverything) { $list += '--verbose' }
    return $list
}

if ($MyInvocation.InvocationName -eq '.' -or $env:ENTRABOT_SETUP_FUNCTIONS_ONLY -eq '1') { return }

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

$showEverything = ($VerbosePreference -ne 'SilentlyContinue')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Strict mode makes reading $LASTEXITCODE an error until some native command has set it.
$global:LASTEXITCODE = 0

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

if ($Status) {
    & (Join-Path $ProjectRoot 'status-windows.ps1') -Json:$Json -HealthOnly:$HealthOnly -Strict:$Strict
    exit $LASTEXITCODE
}

if ($env:OS -ne 'Windows_NT') {
    Write-Host "ERROR: deploy-windows.ps1 must run on Windows." -ForegroundColor Red
    exit 1
}

$venvPython = Join-Path (Join-Path $ProjectRoot '.venv') 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "ERROR: Entrabot is not installed in this folder yet. Run scripts\setup-windows.cmd first." -ForegroundColor Red
    exit 1
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$handoff = @('-m', 'entrabot.winsetup') + @(Get-RotateArguments -ProjectRoot $ProjectRoot -AssumeYes ([bool]$Yes) -ShowEverything $showEverything)
& $venvPython @handoff
exit $LASTEXITCODE
