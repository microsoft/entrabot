<#
.SYNOPSIS
  End-to-end destructive smoke test wrapper for Windows.

.DESCRIPTION
  Delegates to tests/smoke/smokeit.py so Windows and Unix use the same
  orchestration, logs, failure summaries, and cleanup behavior.

  On Windows the runner drives the real entry point unattended:
    powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -NewChain
        -AgentUserUpn <upn> -UseCloudMemory -WithStorageAccount <name>
        -WithContainer <name> -Yes [-Sponsor <upn>]
  Works from Windows PowerShell 5.1 and PowerShell 7.
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SmokeArguments
)

# Windows PowerShell 5.1 writes progress records as CLIXML to stderr when its output is
# captured, which the smoke runner does.
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $PSCommandPath
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
Set-Location $ProjectRoot

$Python = $env:PYTHON
if (-not $Python) {
    $WindowsVenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $UnixVenvPython = Join-Path $ProjectRoot '.venv/bin/python'
    if (Test-Path $WindowsVenvPython) {
        $Python = $WindowsVenvPython
    } elseif (Test-Path $UnixVenvPython) {
        $Python = $UnixVenvPython
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) { throw 'Python 3.12+ is required. Run setup first or set $env:PYTHON.' }
        $Python = $cmd.Source
    }
}

& $Python (Join-Path $ScriptDir 'smokeit.py') @SmokeArguments
exit $LASTEXITCODE
