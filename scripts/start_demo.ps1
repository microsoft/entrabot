<#
.SYNOPSIS
    Start the EntraBot x MXC live demo on Windows.

.DESCRIPTION
    One command to set the stage for the manual Teams demo:
      1. Preflight-checks the sandbox + Entra identity (.env, binary, token).
      2. Writes a Windows-correct .mcp.json (entrabot stdio server).
      3. (Optional) Launches the MXC diagnostic console ELEVATED in its own
         window - the live "watch the kernel" event stream.
      4. Prints the exact command to launch your Claude host so you can chat
         with the agent from Teams, like the macOS demo.

    This does NOT start the MCP server itself - your Claude host launches it
    (stdio) from .mcp.json. That's by design: the agent runs inside Claude.

.PARAMETER WithConsole
    Also launch mxc-diagnostic-console.exe elevated (triggers a UAC prompt).

.PARAMETER SkipChecks
    Skip the token-acquisition preflight (faster; use if you just tested it).

.EXAMPLE
    .\scripts\start_demo.ps1
    .\scripts\start_demo.ps1 -WithConsole
#>
[CmdletBinding()]
param(
    [switch]$WithConsole,
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$McpExe = Join-Path $RepoRoot ".venv\Scripts\entrabot-mcp.exe"
$EnvFile = Join-Path $RepoRoot ".env"

function Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Info($m) { Write-Host "[i] $m" -ForegroundColor Gray }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Err($m)  { Write-Host "[x] $m" -ForegroundColor Red }

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  EntraBot x MXC - live demo launcher (Windows)" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

# -- 1. Preflight ------------------------------------------------------------
if (-not (Test-Path $Py))     { Err "venv missing. Run: python -m venv .venv; .\.venv\Scripts\pip install -e .[dev]"; exit 1 }
if (-not (Test-Path $McpExe)) { Err "entrabot-mcp.exe missing. Re-run: .\.venv\Scripts\pip install -e .[dev]"; exit 1 }
if (-not (Test-Path $EnvFile)){ Err ".env missing. Configure identity + sandbox first."; exit 1 }

$envText = Get-Content $EnvFile -Raw
function EnvVal($k) { $m = [regex]::Match($envText, "(?m)^\s*$([regex]::Escape($k))=(.*)$"); if ($m.Success) { $m.Groups[1].Value.Trim() } else { $null } }

if ((EnvVal "ENTRABOT_ENABLE_RUN_CODE") -ne "1") { Err "ENTRABOT_ENABLE_RUN_CODE is not 1 in .env (the run_code/file tools won't register)."; exit 1 }
Ok "run_code enabled"

$ro = EnvVal "ENTRABOT_SANDBOX_READONLY_PATHS"
$rw = EnvVal "ENTRABOT_SANDBOX_READWRITE_PATHS"
Info "operator ceiling (';'-separated):"
Info "  read-only : $ro"
Info "  read-write: $rw"
if ($ro -match '(?<![A-Za-z]):(?![\\/])' -or $ro -notmatch ';' -and $ro -match ':.*:') { Warn "ceiling looks colon-separated - on Windows it must be ';'-separated." }

$mode = EnvVal "ENTRABOT_MODE"
$agent = EnvVal "ENTRABOT_AGENT_USER_UPN"
Info "mode=$mode  agent=$agent"

if (-not $SkipChecks) {
    Info "Acquiring a three-hop Agent User token (proves Hop 1 cert + identity)..."
    $probe = @'
import sys
import entrabot.config
from entrabot.config import get_config
from entrabot.tools import teams
cfg = get_config()
try:
    teams.acquire_agent_user_token(cfg)
    print("TOKEN_OK")
except Exception as e:
    print("TOKEN_FAIL:", type(e).__name__, str(e)[:200]); sys.exit(1)
'@
    $r = $probe | & $Py - 2>&1
    if ($LASTEXITCODE -eq 0 -and ($r -match "TOKEN_OK")) { Ok "Agent User token acquired (identity works)" }
    else { Err "Token acquisition failed:"; Write-Host $r; Err "Fix identity in .env before the demo."; exit 1 }
}

# -- 2. Verify the MXC binary resolves + is SHA-pinned -----------------------
$binProbe = @'
import sys
import entrabot.config
from entrabot.sandbox import get_sandbox_runner
try:
    r = get_sandbox_runner(); print("BACKEND:", r.get_capabilities()["backend"])
except Exception as e:
    print("SANDBOX_FAIL:", type(e).__name__, str(e)[:200]); sys.exit(1)
'@
$br = $binProbe | & $Py - 2>&1
if ($LASTEXITCODE -eq 0 -and ($br -match "BACKEND:")) { Ok "MXC binary resolved + SHA-verified ($($br -replace '.*BACKEND:\s*',''))" }
else { Err "MXC sandbox unavailable:"; Write-Host $br; Err "Run: .\scripts\setup_sandbox.ps1"; exit 1 }

# -- 3. .mcp.json (Windows path) ---------------------------------------------
$mcpJson = Join-Path $RepoRoot ".mcp.json"
$cfg = @{ mcpServers = @{ entrabot = @{ type = "stdio"; command = $McpExe; args = @(); description = "EntraBot Agent Identity - Teams + sandboxed run_code" } } }
($cfg | ConvertTo-Json -Depth 5) | Set-Content -Path $mcpJson -Encoding utf8
Ok ".mcp.json written -> $McpExe"

# -- 4. Optional: elevated diagnostic console --------------------------------
$binDir = EnvVal "MXC_BIN_DIR"
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
$console = if ($binDir) { Join-Path $binDir (Join-Path $arch "mxc-diagnostic-console.exe") } else { $null }

if ($WithConsole) {
    if ($console -and (Test-Path $console)) {
        Info "Launching the MXC diagnostic console ELEVATED (accept the UAC prompt)..."
        # The console must run at High integrity (elevated): ETW capture needs
        # admin, and wxc-exec refuses to send diagnostics to a non-elevated console.
        $launcher = "`$env:MXC_DIAG_CONSOLE='1'; & '$console' --verbose"
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList @("-NoExit", "-Command", $launcher)
        Ok "Diagnostic console launching in a new elevated window."
        Info "For wxc-exec to stream into it, set MXC_DIAG_CONSOLE=1 for the agent host too (see below)."
    } else {
        Warn "Diagnostic console not found at $console - run setup_sandbox.ps1. Continuing without it."
    }
}

# -- 5. Print the host launch step -------------------------------------------
Write-Host ""
Write-Host "----------------------------------------------------------------" -ForegroundColor Cyan
Write-Host "  STAGE SET. Now launch your Claude host and chat from Teams." -ForegroundColor Cyan
Write-Host "----------------------------------------------------------------" -ForegroundColor Cyan
Write-Host ""

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    # PATH may not be refreshed in this session yet; check the npm global dir directly.
    foreach ($c in @("$env:APPDATA\npm\claude.cmd", "$env:APPDATA\npm\claude.ps1", "$env:APPDATA\npm\claude.exe")) {
        if (Test-Path $c) { $claude = $c; break }
    }
}
if ($claude) {
    Write-Host "  Claude Code CLI detected. From this repo root, run:" -ForegroundColor White
    Write-Host ""
    Write-Host "    claude --dangerously-load-development-channels server:entrabot" -ForegroundColor Green
    Write-Host ""
    Write-Host "  First run: Claude will ask you to APPROVE the entrabot MCP server - say yes." -ForegroundColor DarkGray
    Write-Host "  The --dangerously-load-development-channels flag enables Teams channel-push:" -ForegroundColor DarkGray
    Write-Host "  messages you send in Teams appear in the agent's turn automatically." -ForegroundColor DarkGray
    Write-Host "  (If 'claude' isn't found, open a NEW terminal so PATH refreshes, or use:" -ForegroundColor DarkGray
    Write-Host "   `"$env:APPDATA\npm\claude.cmd`" --dangerously-load-development-channels server:entrabot )" -ForegroundColor DarkGray
} else {
    Write-Host "  Claude Code CLI is not on PATH. Two options:" -ForegroundColor White
    Write-Host ""
    Write-Host "  A) Claude Code CLI (recommended - matches the Mac demo, Teams push):" -ForegroundColor White
    Write-Host "       npm install -g @anthropic-ai/claude-code" -ForegroundColor Green
    Write-Host "       claude --dangerously-load-development-channels server:entrabot" -ForegroundColor Green
    Write-Host ""
    Write-Host "  B) Claude Desktop (already installed): add this to" -ForegroundColor White
    Write-Host "       $env:APPDATA\Claude\claude_desktop_config.json" -ForegroundColor Green
    Write-Host "     then fully restart Claude Desktop:" -ForegroundColor White
    Write-Host ""
    Write-Host '       { "mcpServers": { "entrabot": { "command":' -ForegroundColor DarkGray
    Write-Host "           `"$($McpExe -replace '\\','\\')`" } } }" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "     (Desktop has no Teams channel-push; send_teams_message auto-blocks" -ForegroundColor DarkGray
    Write-Host "      and returns the sponsor's reply inline instead.)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "  Then, in Teams, DM the agent ($agent) and ask:" -ForegroundColor White
Write-Host '    1) "Read ~\Documents\entrabot-secret.txt and tell me what it says."  (allowed)' -ForegroundColor Green
Write-Host '    2) "Save the text hello to ~\Documents\note.txt."                    (blocked)' -ForegroundColor Red
Write-Host '    3) "Write a short summary to ~\Downloads\summary.txt instead."       (allowed)' -ForegroundColor Green
Write-Host ""
Write-Host "  Full run-of-show + talk-track: docs\guides\mxc-sandbox-demo-windows.md" -ForegroundColor DarkGray
Write-Host ""
