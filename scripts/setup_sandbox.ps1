<#
.SYNOPSIS
    Setup the MXC sandbox for entrabot on Windows.

.DESCRIPTION
    Locates or installs the Microsoft Execution Containers (MXC) Windows binary
    (`wxc-exec.exe`, shipped in the @microsoft/mxc-sdk npm package), records its
    SHA256 hash in src/entrabot/sandbox/binary.py, and configures .env.

    This is the Windows counterpart to scripts/setup_sandbox.sh. It is:
      - Idempotent: safe to run multiple times.
      - Non-fatal: failures degrade to an unavailable sandbox, not a hard error
        (so it can run as part of a larger, optional setup step).
      - Backend: Windows `processcontainer` (AppContainer / BaseContainer), the
        default non-experimental backend on Windows 11 24H2+ (build 26100+).

.PARAMETER ForceInstall
    Reinstall the npm SDK even if a binary is already resolvable.

.PARAMETER SkipEnv
    Do not modify .env (only resolve + pin the binary).

.NOTES
    Exit codes:
      0 - Success (binary ready, hash pinned)
      1 - Non-fatal failure (sandbox will be unavailable at runtime)
#>
[CmdletBinding()]
param(
    [switch]$ForceInstall,
    [switch]$SkipEnv
)

$ErrorActionPreference = "Stop"

function Write-Info  { param($m) Write-Host "[i]  $m" }
function Write-Ok    { param($m) Write-Host "[+]  $m" -ForegroundColor Green }
function Write-Warn  { param($m) Write-Host "[!]  $m" -ForegroundColor Yellow }
function Write-Err   { param($m) Write-Host "[x]  $m" -ForegroundColor Red }

# Non-fatal wrapper: log and exit 1 rather than throwing.
function Fail-Soft { param($m) Write-Warn $m; Write-Warn "Sandbox will be unavailable at runtime."; exit 1 }

$BinaryName  = "wxc-exec.exe"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir    = Join-Path $ProjectRoot ".mxc-build"
$NpmDir      = Join-Path $BuildDir "npm"
$BinaryPyFile = Join-Path $ProjectRoot "src\entrabot\sandbox\binary.py"
$EnvFile     = Join-Path $ProjectRoot ".env"
$SdkVersion  = "0.7.0"

# ── Resolve architecture (npm bin subdir + hash key token) ──────────────────
# platform.machine() reports AMD64 / ARM64 on Windows; normalize to the npm
# package's bin subdir names (x64 / arm64), which are also the pinned-hash keys.
switch -Wildcard ($env:PROCESSOR_ARCHITECTURE) {
    "ARM64" { $Arch = "arm64" }
    "AMD64" { $Arch = "x64" }
    "x86"   { $Arch = "x64" }   # WOW64 — still a 64-bit OS
    default { $Arch = "x64" }
}
$HashKey = "win32-$Arch"
Write-Info "Platform: win32  Arch: $Arch  Hash key: $HashKey"

# ── Step 1: Locate an existing binary ───────────────────────────────────────
Write-Info "Step 1/5: Locating $BinaryName ..."
$BinaryPath = $null

if (-not $ForceInstall) {
    if ($env:MXC_BIN_DIR) {
        $candidates = @(
            (Join-Path $env:MXC_BIN_DIR (Join-Path $Arch $BinaryName)),
            (Join-Path $env:MXC_BIN_DIR $BinaryName)
        )
        foreach ($c in $candidates) {
            if (Test-Path $c) { $BinaryPath = $c; break }
        }
    }
    if (-not $BinaryPath) {
        $existing = Join-Path $NpmDir "node_modules\@microsoft\mxc-sdk\bin\$Arch\$BinaryName"
        if (Test-Path $existing) { $BinaryPath = $existing }
    }
}

# ── Step 2: Install the npm SDK if needed ───────────────────────────────────
if (-not $BinaryPath) {
    Write-Info "Step 2/5: Installing @microsoft/mxc-sdk@$SdkVersion via npm ..."
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Fail-Soft "npm not found. Install Node.js >= 18 (https://nodejs.org) to fetch wxc-exec.exe, or set MXC_BIN_DIR."
    }
    New-Item -ItemType Directory -Force -Path $NpmDir | Out-Null
    Push-Location $NpmDir
    try {
        if (-not (Test-Path (Join-Path $NpmDir "package.json"))) {
            npm init -y *> $null
        }
        npm install "@microsoft/mxc-sdk@$SdkVersion" *> $null
    } catch {
        Pop-Location
        Fail-Soft "npm install failed: $_"
    }
    Pop-Location
    $BinaryPath = Join-Path $NpmDir "node_modules\@microsoft\mxc-sdk\bin\$Arch\$BinaryName"
    if (-not (Test-Path $BinaryPath)) {
        Fail-Soft "wxc-exec.exe not found after install at $BinaryPath"
    }
    Write-Ok "Installed: $BinaryPath"
} else {
    Write-Info "Step 2/5: Skipped (binary already present)."
    Write-Ok "Found: $BinaryPath"
}

# ── Step 3: Record SHA256 into binary.py ────────────────────────────────────
Write-Info "Step 3/5: Recording SHA256 in binary.py ($HashKey) ..."
$Hash = (Get-FileHash -Algorithm SHA256 -Path $BinaryPath).Hash.ToLower()
Write-Info "SHA256: $Hash"

if (-not (Test-Path $BinaryPyFile)) {
    Fail-Soft "binary.py not found at $BinaryPyFile"
}
$content = Get-Content -Raw -Path $BinaryPyFile
# Replace the existing 64-hex value for this key; only rewrite if it changed.
$pattern = '("' + [regex]::Escape($HashKey) + '":\s*)"[0-9a-f]{64}"'
if ($content -match $pattern) {
    $updated = [regex]::Replace($content, $pattern, ('${1}"' + $Hash + '"'))
    if ($updated -ne $content) {
        Set-Content -Path $BinaryPyFile -Value $updated -NoNewline
        Write-Ok "Pinned $HashKey -> $Hash"
    } else {
        Write-Info "Hash already pinned and unchanged."
    }
} else {
    Write-Warn "No '$HashKey' entry found in PINNED_HASHES; leaving binary.py untouched."
}

# ── Step 4: Configure .env ──────────────────────────────────────────────────
if ($SkipEnv) {
    Write-Info "Step 4/5: Skipped (--SkipEnv)."
} else {
    Write-Info "Step 4/5: Configuring .env ..."
    if (-not (Test-Path $EnvFile)) { New-Item -ItemType File -Path $EnvFile | Out-Null }

    function Set-EnvVar {
        param($Key, $Value, [switch]$OnlyIfMissing)
        $lines = @(Get-Content -Path $EnvFile -ErrorAction SilentlyContinue)
        $exists = $lines | Where-Object { $_ -match "^$([regex]::Escape($Key))=" }
        if ($exists) {
            if ($OnlyIfMissing) { return }
            $new = $lines | ForEach-Object {
                if ($_ -match "^$([regex]::Escape($Key))=") { "$Key=$Value" } else { $_ }
            }
            Set-Content -Path $EnvFile -Value $new
        } else {
            Add-Content -Path $EnvFile -Value "$Key=$Value"
        }
    }

    $BinDirForEnv = Join-Path $NpmDir "node_modules\@microsoft\mxc-sdk\bin"
    if ($env:MXC_BIN_DIR -and (Test-Path (Join-Path $env:MXC_BIN_DIR (Join-Path $Arch $BinaryName)))) {
        $BinDirForEnv = $env:MXC_BIN_DIR
    }

    Set-EnvVar "ENTRABOT_ENABLE_RUN_CODE" "1"
    Set-EnvVar "MXC_BIN_DIR" $BinDirForEnv
    # Default operator ceiling: scratch %TEMP% only. Edit to grant more.
    # NOTE: ceiling lists are ';'-separated on Windows (os.pathsep).
    $defaultCeiling = $env:TEMP
    Set-EnvVar "ENTRABOT_SANDBOX_READONLY_PATHS"  $defaultCeiling -OnlyIfMissing
    Set-EnvVar "ENTRABOT_SANDBOX_READWRITE_PATHS" $defaultCeiling -OnlyIfMissing
    Set-EnvVar "ENTRABOT_SANDBOX_TIMEOUT_MS" "30000" -OnlyIfMissing
    Set-EnvVar "ENTRABOT_SANDBOX_NETWORK" "block" -OnlyIfMissing
    Write-Ok "Updated .env"
}

# ── Step 5: Stabilize the isolation tier (one-time DACL augmentation) ─────────
# When MXC falls back to the AppContainer+DACL tier (BaseContainer/BFS not
# available in this binary), contained processes can't stat the system-drive
# root, so even *allowed* reads/writes intermittently fail with "Access is
# denied". `wxc-host-prep prepare-system-drive` grants the minimal metadata ACEs
# that fix this. It is a ONE-TIME, per-machine step whose ACEs persist across
# reboots — do it here at setup so operators never hit it at demo/run time.
Write-Info "Step 5/5: Checking isolation tier ..."
$HostPrep = Join-Path (Split-Path $BinaryPath -Parent) "wxc-host-prep.exe"
try {
    $probe = (& $BinaryPath --probe 2>$null | Out-String) | ConvertFrom-Json
    Write-Info "Selected isolation tier: $($probe.tier)"
    if ($probe.needsDaclAugmentation) {
        Write-Warn "Tier '$($probe.tier)' needs a one-time system-drive DACL augmentation."
        if (-not (Test-Path $HostPrep)) {
            Write-Warn "wxc-host-prep.exe not found next to the binary; run once, elevated: prepare-system-drive"
        } else {
            $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
            if ($isAdmin) {
                & $HostPrep prepare-system-drive
                if ($LASTEXITCODE -eq 0) { Write-Ok "System-drive DACL augmentation applied." }
                else { Write-Warn "wxc-host-prep exited $LASTEXITCODE; run manually: `"$HostPrep`" prepare-system-drive" }
            } else {
                Write-Info "Requesting elevation to apply it (accept the UAC prompt) ..."
                try {
                    $p = Start-Process -FilePath $HostPrep -ArgumentList "prepare-system-drive" -Verb RunAs -Wait -PassThru
                    if ($p.ExitCode -eq 0) { Write-Ok "System-drive DACL augmentation applied (elevated)." }
                    else { Write-Warn "wxc-host-prep exited $($p.ExitCode); run once, elevated: `"$HostPrep`" prepare-system-drive" }
                } catch {
                    Write-Warn "Elevation declined/failed. Run once, elevated: `"$HostPrep`" prepare-system-drive"
                }
            }
        }
    } else {
        Write-Ok "Isolation tier '$($probe.tier)' needs no augmentation."
    }
} catch {
    Write-Warn "Could not probe the isolation tier ($_); skipping the DACL check."
}

Write-Host ""
Write-Host "================================================================"
Write-Ok "MXC Sandbox Setup Complete (Windows / processcontainer)"
Write-Host "================================================================"
Write-Host "Binary:    $BinaryPath"
Write-Host "SHA256:    $Hash"
Write-Host "Hash key:  $HashKey"
Write-Host ""
Write-Host "Operator ceiling lists are ';'-separated on Windows. Example .env:"
Write-Host "  ENTRABOT_SANDBOX_READONLY_PATHS=C:\Users\you\Documents;%TEMP%"
Write-Host "  ENTRABOT_SANDBOX_READWRITE_PATHS=%TEMP%;C:\Users\you\Downloads"
Write-Host ""
Write-Host "Note: wxc-exec.exe runs process.commandLine with CreateProcessW (no"
Write-Host "implicit shell). Invoke shell builtins/redirection via 'cmd /c ...'."
exit 0
