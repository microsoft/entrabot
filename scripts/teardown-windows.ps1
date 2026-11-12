<#
.SYNOPSIS
  Entrabot - disconnect this computer from the agent (Windows).

.DESCRIPTION
  By default this DISCONNECTS THIS COMPUTER and nothing else:

    - This computer's certificate is removed from the agent's Blueprint in Microsoft
      Entra, so it stops being trusted now instead of when it expires. The agent's other
      computers keep working.
    - The certificate and its private key are deleted from this computer. It is chosen by
      the thumbprint in .env, so another agent's certificate is never touched.
    - The "entrabot" entry is removed from .mcp.json and from GitHub Copilot CLI's
      mcp-config.json. Every other entry is kept.
    - .env and .entrabot-state.json are backed up next to the originals, then removed.
    - This computer's setup (Provisioner) certificate is retired: removed from the
      Provisioner app in Microsoft Entra and deleted here with its private key. Setup signs
      in with it and it can manage applications in your tenant, so it should not stay on a
      computer you are done with. It is kept only if another Entrabot agent on this
      computer still uses it, or if you pass -KeepSetupCredential.

  The agent itself, its memory, and Entrabot's data folder are NOT touched unless you ask:

    -DeleteAgent   also deletes the agent from your tenant (Agent User, Agent Identity,
                   Blueprint) for every computer that uses it. This cannot be undone.
    -PurgeData     also deletes Entrabot's data folder on this computer
                   (%LOCALAPPDATA%\entrabot): local memory, logs, and Entrabot's private
                   copies of Python and the Azure CLI. The project's .venv then needs
                   scripts\setup-windows.cmd to rebuild it.

  It says what it will remove and asks first. If Microsoft Entra cannot be reached, the
  local cleanup still completes and the report says exactly what is still listed there and
  how to remove it.

  This script is a thin wrapper. The work is done by:
      python -m entrabot.winsetup teardown
  It runs on the Windows PowerShell 5.1 that ships with Windows, and on PowerShell 7.

  Exit codes: 0 done or nothing to do, 1 finished with something left to do, 2 usage,
  130 cancelled.

.PARAMETER DeleteAgent
  Also delete the agent from the tenant. Cannot be undone.

.PARAMETER PurgeData
  Also delete Entrabot's data folder on this computer.

.PARAMETER KeepSetupCredential
  Keep this computer's setup (Provisioner) certificate. Use it when you are about to set
  this computer up again; otherwise let teardown retire it.

.PARAMETER Yes
  Do not ask. Only the scopes named by the other parameters are removed.

.PARAMETER Force
  Older name for -Yes.

.PARAMETER Help
  Show this help.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\teardown-windows.ps1 -DeleteAgent
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$DeleteAgent,
    [switch]$PurgeData,
    [switch]$KeepSetupCredential,
    [switch]$Yes,
    [switch]$Force,
    [switch]$Help
)

# Windows PowerShell 5.1 writes progress records as CLIXML to stderr when its output is
# not a console. Nothing here needs a progress bar.
$ProgressPreference = 'SilentlyContinue'

# =============================================================================
# Pure helpers, loadable by the tests (dot-source this file). This file must stay
# ASCII-only: Windows PowerShell 5.1 reads a file without a byte-order mark in the
# ANSI code page, where one non-ASCII character is a parse error.
# =============================================================================

function Get-TeardownArguments {
    # The argument list handed to `python -m entrabot.winsetup`. Values are separate
    # array elements, so nothing is ever quoted by hand.
    param(
        [string]$ProjectRoot,
        [bool]$DeleteAgent,
        [bool]$PurgeData,
        [bool]$KeepSetupCredential,
        [bool]$AssumeYes,
        [bool]$ShowEverything
    )
    $list = @('teardown', '--project-root', $ProjectRoot)
    if ($DeleteAgent) { $list += '--delete-agent' }
    if ($PurgeData) { $list += '--purge-data' }
    if ($KeepSetupCredential) { $list += '--keep-setup-credential' }
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

if ($env:OS -ne 'Windows_NT') {
    Write-Host "ERROR: teardown-windows.ps1 must run on Windows. On macOS or Linux run scripts/teardown.sh." -ForegroundColor Red
    exit 1
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$venvPython = Join-Path (Join-Path $ProjectRoot '.venv') 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "ERROR: Entrabot is not installed in this folder (no .venv), so there is nothing here for teardown to run with. Run scripts\setup-windows.cmd -BootstrapOnly, then run teardown again." -ForegroundColor Red
    exit 1
}

$assumeYes = ([bool]$Yes) -or ([bool]$Force)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$handoff = @('-m', 'entrabot.winsetup') + @(Get-TeardownArguments -ProjectRoot $ProjectRoot -DeleteAgent ([bool]$DeleteAgent) -PurgeData ([bool]$PurgeData) -KeepSetupCredential ([bool]$KeepSetupCredential) -AssumeYes $assumeYes -ShowEverything $showEverything)
& $venvPython @handoff
exit $LASTEXITCODE
