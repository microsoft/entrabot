<#
.SYNOPSIS
  End-to-end destructive smoke test wrapper for Windows.

.DESCRIPTION
  Delegates to tests/smoke/smokeit.py so Windows and Unix use the same
  orchestration, logs, failure summaries, and cleanup behavior.
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

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

& $Python (Join-Path $ScriptDir 'smokeit.py') @Args
exit $LASTEXITCODE
