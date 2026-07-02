<#
.SYNOPSIS
    EntraBot x MXC - least-privilege local-execution demo (Windows).

.DESCRIPTION
    The Windows counterpart to scripts/demo_sandbox.py. Drives the REAL,
    SHA256-pinned wxc-exec.exe through the exact run_code enforcement chain the
    MCP server uses (operator ceiling -> clamp -> canonicalize -> MXC
    processcontainer) and narrates each beat so an audience can watch the
    Windows kernel - not Python, not the agent's good behavior - enforce the
    boundary.

    Pair it with an ELEVATED mxc-diagnostic-console.exe in a second window to
    show the live event stream (see docs/guides/mxc-sandbox-demo-windows.md).

.PARAMETER NoPause
    Run straight through without pausing between beats (for recording / CI).

.PARAMETER ConfigOnly
    Print the operator ceiling + backend and exit (the operator's view).

.NOTES
    Requires:
      - ENTRABOT_ENABLE_RUN_CODE=1 and the MXC sandbox vars in .env
      - The real wxc-exec.exe resolvable via MXC_BIN_DIR (run setup_sandbox.ps1)
      - The repo venv at .venv\Scripts\python.exe
#>
[CmdletBinding()]
param(
    [switch]$NoPause,
    [switch]$ConfigOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Runner = Join-Path $PSScriptRoot "demo_sandbox_run.py"

$Docs = Join-Path $HOME "Documents"
$Downloads = Join-Path $HOME "Downloads"
$Temp = $env:TEMP

function Banner($text) {
    $line = "=" * 64
    Write-Host ""
    Write-Host $line -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host $line -ForegroundColor Cyan
}

function Beat($text) {
    if ($NoPause) { Write-Host "`n  -> $text" -ForegroundColor DarkGray }
    else { Read-Host "`n  [Enter] $text" | Out-Null }
}

function Invoke-Scenario {
    param(
        [string]$Title, [string]$Say, [string]$Cmd,
        [string[]]$Ro = @(), [string[]]$Rw = @(), [bool]$ExpectAllow,
        [string]$ReadBack = $null
    )
    Write-Host ""
    Write-Host "  $Title" -ForegroundColor Blue
    Write-Host "    $Say" -ForegroundColor Gray
    Write-Host "    agent runs   : $Cmd" -ForegroundColor DarkGray
    $reqRo = if ($Ro.Count) { $Ro -join ', ' } else { '[]' }
    $reqRw = if ($Rw.Count) { $Rw -join ', ' } else { '[]' }
    Write-Host "    agent asks for: read=$reqRo  write=$reqRw" -ForegroundColor DarkGray

    $argList = @($Runner, "--cmd", $Cmd)
    foreach ($p in $Ro) { $argList += @("--ro", $p) }
    foreach ($p in $Rw) { $argList += @("--rw", $p) }
    $json = & $Py @argList | Select-Object -Last 1
    $r = $json | ConvertFrom-Json

    if ($r.error) {
        Write-Host "    HARNESS ERROR: $($r.error)" -ForegroundColor Red
        return $false
    }

    if ($r.dropped_rw -and $r.dropped_rw.Count) {
        Write-Host "    clamp        : dropped WRITE $($r.dropped_rw -join ', ') (outside operator ceiling)" -ForegroundColor Yellow
    }
    if ($r.dropped_ro -and $r.dropped_ro.Count) {
        Write-Host "    clamp        : dropped READ $($r.dropped_ro -join ', ') (outside operator ceiling)" -ForegroundColor Yellow
    }
    $sentRo = if ($r.clamped_ro.Count) { $r.clamped_ro -join ', ' } else { '[]' }
    $sentRw = if ($r.clamped_rw.Count) { $r.clamped_rw -join ', ' } else { '[]' }
    Write-Host "    policy -> MXC: read=$sentRo  write=$sentRw" -ForegroundColor DarkGray

    if ($r.allowed) {
        $detail = if ($r.stdout) { $r.stdout } else { "(no output)" }
        # For write scenarios the write goes to a file (no stdout); read it back to prove it landed.
        if ($ReadBack -and (Test-Path $ReadBack)) { $detail = (Get-Content $ReadBack -Raw).Trim() }
        Write-Host "    [+] ALLOWED  exit=$($r.exit_code)  output: $detail" -ForegroundColor Green
    } else {
        $detail = if ($r.stderr) { $r.stderr } else { "(blocked)" }
        Write-Host "    [x] BLOCKED by the Windows kernel  exit=$($r.exit_code)  reason: $detail" -ForegroundColor Red
    }

    $correct = ($r.allowed -eq $ExpectAllow)
    $expect = if ($ExpectAllow) { "ALLOW" } else { "BLOCK" }
    if ($correct) { Write-Host "    expected $expect -> as designed" -ForegroundColor Green }
    else { Write-Host "    expected $expect -> UNEXPECTED" -ForegroundColor Red }
    return $correct
}

# -- Preconditions -----------------------------------------------------------
if (-not (Test-Path $Py)) { Write-Host "venv not found at $Py. Run: python -m venv .venv; .venv\Scripts\pip install -e .[dev]" -ForegroundColor Red; exit 1 }
if ($env:ENTRABOT_ENABLE_RUN_CODE -ne "1") {
    # .env may set it; the Python runner loads .env, so just warn.
    Write-Host "(note: ENTRABOT_ENABLE_RUN_CODE not set in this shell; .env value will be used by the runner)" -ForegroundColor DarkYellow
}

Banner "EntraBot x MXC - Least-Privilege Local Execution (Windows)"
Write-Host @"

  An AI agent with its own Entra identity wants to run code on this PC.
  The OPERATOR decides what it may touch. The agent can only NARROW that -
  never widen it. Containment is enforced by Windows' processcontainer
  (AppContainer) via Microsoft Execution Containers (MXC).
"@

# Show ceiling + backend by running a trivial probe through the real chain.
$probe = (& $Py $Runner --cmd "cmd /c echo ." | Select-Object -Last 1) | ConvertFrom-Json
if ($probe.error) { Write-Host "`nMXC unavailable: $($probe.error)" -ForegroundColor Red; Write-Host "Run scripts\setup_sandbox.ps1 first." -ForegroundColor DarkGray; exit 1 }
Write-Host "`n  Operator ceiling (the human-set maximum):" -ForegroundColor White
Write-Host "    read-only : $($probe.ceiling_ro -join ', ')" -ForegroundColor Green
Write-Host "    read-write: $($probe.ceiling_rw -join ', ')" -ForegroundColor Green
Write-Host "    keychain  : hard-disabled (not overridable by the agent)" -ForegroundColor DarkGray
Write-Host "`n  Backend: $($probe.backend) (real binary, SHA256-verified)" -ForegroundColor White
$agent = if ($env:ENTRABOT_AGENT_USER_UPN) { $env:ENTRABOT_AGENT_USER_UPN } else {
    # Not in the shell env; the runner reads .env, so surface it here too.
    $envFile = Join-Path $RepoRoot ".env"
    $val = "(unset)"
    if (Test-Path $envFile) {
        $m = Select-String -Path $envFile -Pattern '^\s*ENTRABOT_AGENT_USER_UPN=(.+)$' | Select-Object -First 1
        if ($m) { $val = $m.Matches[0].Groups[1].Value.Trim() }
    }
    $val
}
Write-Host "  Agent identity: $agent (its own Entra Agent User)" -ForegroundColor White

if ($ConfigOnly) {
    Write-Host "`n  This is the operator-set configuration. The agent can only narrow it." -ForegroundColor DarkGray
    Write-Host "  Run without -ConfigOnly to see it enforced.`n"
    exit 0
}

# Fixture: an informational file in Documents the agent may READ but not WRITE.
New-Item -ItemType Directory -Force $Docs | Out-Null
$InfoFile = Join-Path $Docs "entrabot-info.txt"
if (-not (Test-Path $InfoFile)) {
    # ASCII (no BOM) so `cmd /c type` doesn't show stray BOM bytes in the demo.
    Set-Content -Path $InfoFile -Value "EntraBot demo file - quarterly figures the agent may read but must not alter" -Encoding ascii
}
New-Item -ItemType Directory -Force $Downloads | Out-Null
Write-Host "`n  Fixture ready: $InfoFile" -ForegroundColor DarkGray

$results = @()

Banner "Act 1 - The agent reads what you allow"
Beat "Scenario 1 - read your Documents (legitimate analysis)"
$results += Invoke-Scenario -Title '"Read my file in Documents."' `
    -Say "Documents is in my read-only ceiling, so this is allowed." `
    -Cmd ('cmd /c type "' + $InfoFile + '"') -Ro @($Docs) -ExpectAllow $true

Banner "Act 2 - The agent cannot tamper"
$hackFile = Join-Path $Docs "entrabot-hack.txt"
Beat "Scenario 2 - try to WRITE to your Documents (tampering)"
$results += Invoke-Scenario -Title '"Overwrite a file in Documents."' `
    -Say "Documents is NOT in my read-write ceiling. The clamp drops it to [] and the kernel blocks the write." `
    -Cmd ('cmd /c echo TAMPERED > "' + $hackFile + '"') -Rw @($Docs) -ExpectAllow $false

Banner "Act 3 - The agent writes only where you allow"
$reportFile = Join-Path $Temp "entrabot-report.txt"
Beat "Scenario 3 - write a scratch report to %TEMP%"
$results += Invoke-Scenario -Title '"Save a scratch report to my temp folder."' `
    -Say "TEMP is in my read-write ceiling." `
    -Cmd ('cmd /c echo scratch report > "' + $reportFile + '"') -Rw @($Temp) -ExpectAllow $true -ReadBack $reportFile

$exportFile = Join-Path $Downloads "entrabot-export.txt"
Beat "Scenario 4 - write an export to your Downloads"
$results += Invoke-Scenario -Title '"Drop the export in my Downloads folder."' `
    -Say "Downloads is in my read-write ceiling." `
    -Cmd ('cmd /c echo export data > "' + $exportFile + '"') -Rw @($Downloads) -ExpectAllow $true -ReadBack $exportFile

Banner "Act 4 - The agent can't reach the OS"
Beat "Scenario 5 - try to write into C:\Windows (system tampering)"
$results += Invoke-Scenario -Title '"Write into the Windows system directory."' `
    -Say "C:\Windows isn't in any ceiling. The clamp drops it and the kernel blocks it." `
    -Cmd 'cmd /c echo OWNED > C:\Windows\entrabot-owned.txt' -Rw @("C:\Windows") -ExpectAllow $false

# Cleanup
foreach ($p in @($hackFile, $reportFile, $exportFile, "C:\Windows\entrabot-owned.txt")) {
    Remove-Item $p -ErrorAction SilentlyContinue
}

Banner "Recap"
$passed = ($results | Where-Object { $_ }).Count
$total = $results.Count
Write-Host ""
Write-Host "  READ Documents allowed - WRITE Documents blocked - WRITE TEMP + Downloads allowed - WRITE C:\Windows blocked" -ForegroundColor White
Write-Host ""
Write-Host "  Every action is audit-first: logged before it runs, and if audit cannot" -ForegroundColor Gray
Write-Host "  record, the action does not proceed. Fail-closed, and attributed to the" -ForegroundColor Gray
Write-Host "  agent own Entra identity - not yours." -ForegroundColor Gray
$color = if ($passed -eq $total) { "Green" } else { "Red" }
Write-Host "`n  $passed/$total scenarios behaved exactly as designed." -ForegroundColor $color

# -- Agent first-person Teams talk-track -------------------------------------
Banner "Now do it live - Teams talk-track"
Write-Host ""
Write-Host "  Chat with the agent ($agent) in Teams and ask, in plain language."
Write-Host "  The agent calls run_code / read_local_file / write_local_file under the hood."
Write-Host ""
Write-Host '  1) "Read my file at ~\Documents\entrabot-info.txt and tell me what it says."' -ForegroundColor Green
Write-Host "       -> Agent reads it. Point out: Documents is read-only in the ceiling." -ForegroundColor DarkGray
Write-Host ""
Write-Host '  2) "Now save the text hello to ~\Documents\note.txt."' -ForegroundColor Red
Write-Host "       -> Blocked. The agent reports it cannot write there. Show the audit log." -ForegroundColor DarkGray
Write-Host ""
Write-Host '  3) "Write a short summary to ~\Downloads\summary.txt instead."' -ForegroundColor Green
Write-Host "       -> Works. Downloads is in the read-write ceiling." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  The agent never sees the ceiling as something it can change - you, the" -ForegroundColor DarkGray
Write-Host "  operator, set it in .env, and the OS enforces it. The model can only narrow." -ForegroundColor DarkGray

exit $(if ($passed -eq $total) { 0 } else { 1 })
