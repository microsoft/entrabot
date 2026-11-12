<#
.SYNOPSIS
  Entrabot - optional extras for Windows. You do NOT need to run this before setup.

.DESCRIPTION
  scripts\setup-windows.cmd installs everything Entrabot itself needs (Python and the
  project's packages) without administrator rights, and the setup wizard takes care of
  the Azure CLI. This helper only installs the extras, and only the ones you ask for:

    -WithGit          Git for Windows, for your account only. Claude Code runs this
                      project's safety hooks through Git Bash, so it is needed if you use
                      Claude Code. No administrator rights and no winget required.
    -WithClaudeCode   Claude Code, using its official installer.
    -WithWorkIq       .NET SDK and the Microsoft Agent 365 CLI (a365), needed only for
                      scripts\setup-windows.cmd -ConfigureA365WorkIq.
    -WithBuildTools   Visual Studio Build Tools (C++), about 6 GB. Only needed if setup
                      reports that a package had to be compiled.

  Run with no switch to see this summary. Safe to re-run: anything already installed is
  left alone. Each switch only reports success when the tool actually runs afterwards.
  The setup wizard calls -WithGit and -WithClaudeCode for you when you ask it to.

  Works on the Windows PowerShell 5.1 that ships with Windows. -WithWorkIq and
  -WithBuildTools use winget and their installers may ask for administrator approval.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithGit -WithClaudeCode

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithWorkIq
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$WithGit,
    [switch]$WithClaudeCode,
    [switch]$WithWorkIq,
    [switch]$WithBuildTools,
    [switch]$SkipBuildTools,
    [switch]$Help
)

# This file must stay ASCII-only (Windows PowerShell 5.1 reads it in the ANSI code page).

# Windows PowerShell 5.1 writes progress records as CLIXML to stderr when its output is
# not a console (SSH, CI, a captured run), and its progress bar slows downloads badly.
$ProgressPreference = 'SilentlyContinue'

# Used when the latest release cannot be looked up (GitHub's API is rate limited).
$PinnedGitVersion = '2.56.1'
# Git for Windows releases are signed by the project's maintainer.
$GitPublisher = 'Johannes Schindelin'

function Test-WingetSuccess {
    # winget reports "already installed" and "nothing newer" as non-zero exit codes.
    param([int]$ExitCode)
    $okCodes = @(
        0,
        -1978335189,   # 0x8A15002B  no applicable update found
        -1978335135,   # 0x8A150061  package already installed
        -1978335153,   # 0x8A15004F  upgrade version is not newer
        -1978334963    # 0x8A15010D  another version is already installed
    )
    return ($okCodes -contains $ExitCode)
}

function ConvertFrom-DistinguishedName {
    # Splits a certificate subject such as  CN=Name, O="Org, Inc.", C=US  into its
    # attribute=value parts the way .NET writes it: parts are separated by , ; or + outside
    # quotes, a value may be "quoted" ("" is a quote inside one), and \ escapes the next
    # character. Returns an array of @{ Key; Value }, or $null when the text is not a
    # well-formed name (a subject that cannot be read is never trusted).
    # Same as ConvertFrom-DistinguishedName in setup-windows.ps1; keep the two in step.
    param([string]$Subject)
    if (-not $Subject) { return $null }
    $text = $Subject
    $n = $text.Length
    $i = 0
    $parts = New-Object 'System.Collections.Generic.List[object]'
    while ($i -lt $n) {
        $equals = $text.IndexOf('=', $i)
        if ($equals -lt 0) { return $null }
        $key = $text.Substring($i, $equals - $i).Trim()
        if ($key -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') { return $null }
        $i = $equals + 1
        while ($i -lt $n -and $text[$i] -eq ' ') { $i++ }
        $value = New-Object System.Text.StringBuilder
        if ($i -lt $n -and $text[$i] -eq '"') {
            $i++
            $closed = $false
            while ($i -lt $n) {
                $character = $text[$i]
                if ($character -eq '"') {
                    if ($i + 1 -lt $n -and $text[$i + 1] -eq '"') {
                        [void]$value.Append('"')
                        $i += 2
                        continue
                    }
                    $closed = $true
                    $i++
                    break
                }
                if ($character -eq '\') {
                    if ($i + 1 -ge $n) { return $null }
                    [void]$value.Append($text[$i + 1])
                    $i += 2
                    continue
                }
                [void]$value.Append($character)
                $i++
            }
            if (-not $closed) { return $null }
            while ($i -lt $n -and $text[$i] -eq ' ') { $i++ }
            $valueText = $value.ToString()
        } else {
            while ($i -lt $n) {
                $character = $text[$i]
                if ($character -eq ',' -or $character -eq ';' -or $character -eq '+') { break }
                if ($character -eq '"') { return $null }
                if ($character -eq '\') {
                    if ($i + 1 -ge $n) { return $null }
                    [void]$value.Append($text[$i + 1])
                    $i += 2
                    continue
                }
                [void]$value.Append($character)
                $i++
            }
            $valueText = $value.ToString().Trim()
        }
        $parts.Add([pscustomobject]@{ Key = $key; Value = $valueText })
        if ($i -ge $n) { break }
        $separator = $text[$i]
        if ($separator -ne ',' -and $separator -ne ';' -and $separator -ne '+') { return $null }
        $i++
        if ($i -ge $n) { return $null }   # a trailing separator
    }
    return ,$parts.ToArray()
}

function Test-TrustedPublisher {
    # A download is only run when Windows says its signature is valid AND the signer's
    # subject has exactly one organization (O=) and it is exactly the expected one. The
    # subject is parsed, not searched: a quoted CN can contain the text ", O=...,".
    param([string]$Status, [string]$Subject, [string]$Organization)
    if ($Status -ne 'Valid') { return $false }
    if (-not $Organization) { return $false }
    $parts = ConvertFrom-DistinguishedName -Subject $Subject
    if ($null -eq $parts) { return $false }
    $organizations = @()
    foreach ($part in $parts) {
        if (@('O', 'OID.2.5.4.10', '2.5.4.10') -contains $part.Key) { $organizations += [string]$part.Value }
    }
    if ($organizations.Count -ne 1) { return $false }
    return ($organizations[0] -ceq $Organization)
}

function Get-MachineArchitecture {
    # PROCESSOR_ARCHITEW6432 is set when a 32-bit shell runs on a 64-bit Windows.
    param([string]$ProcessorArchitecture, [string]$ProcessorArchiteW6432)
    $raw = $ProcessorArchiteW6432
    if (-not $raw) { $raw = $ProcessorArchitecture }
    if (-not $raw) { return 'x86' }
    switch ($raw.ToUpperInvariant()) {
        'AMD64' { return 'amd64' }
        'ARM64' { return 'arm64' }
        default { return 'x86' }
    }
}

function Get-GitAssetPattern {
    # The exact file name of a Git for Windows release download.
    param([string]$Kind, [string]$Architecture)
    $tag = $null
    if ($Architecture -eq 'amd64') { $tag = '64-bit' }
    if ($Architecture -eq 'arm64') { $tag = 'arm64' }
    if (-not $tag) { return $null }
    if ($Kind -eq 'installer') { return ('^Git-\d+(\.\d+)+-{0}\.exe$' -f $tag) }
    if ($Kind -eq 'portable') { return ('^PortableGit-\d+(\.\d+)+-{0}\.7z\.exe$' -f $tag) }
    return $null
}

function Get-PinnedGitUrl {
    param([string]$Kind, [string]$Architecture)
    $tag = $null
    if ($Architecture -eq 'amd64') { $tag = '64-bit' }
    if ($Architecture -eq 'arm64') { $tag = 'arm64' }
    if (-not $tag) { return $null }
    $base = 'https://github.com/git-for-windows/git/releases/download/v{0}.windows.1' -f $PinnedGitVersion
    if ($Kind -eq 'installer') { return ('{0}/Git-{1}-{2}.exe' -f $base, $PinnedGitVersion, $tag) }
    if ($Kind -eq 'portable') { return ('{0}/PortableGit-{1}-{2}.7z.exe' -f $base, $PinnedGitVersion, $tag) }
    return $null
}

function Select-GitAssetUrl {
    # Picks one download from a GitHub release's asset list (objects with name and
    # browser_download_url). Only the project's own release downloads are accepted.
    param([object[]]$Assets, [string]$Kind, [string]$Architecture)
    $pattern = Get-GitAssetPattern -Kind $Kind -Architecture $Architecture
    if (-not $pattern) { return $null }
    foreach ($asset in @($Assets)) {
        if ($null -eq $asset) { continue }
        $name = [string]$asset.name
        $url = [string]$asset.browser_download_url
        if ($name -notmatch $pattern) { continue }
        if (-not $url.StartsWith('https://github.com/git-for-windows/git/releases/download/')) { continue }
        return $url
    }
    return $null
}

function Join-WindowsPath {
    # Plain string join. Join-Path and Split-Path throw "Cannot find drive" for a path on
    # a drive that is not there (an unplugged disk, a stale environment variable).
    param([string]$Base, [string]$Child)
    return ($Base.TrimEnd('\') + '\' + $Child)
}

function Get-WindowsParent {
    param([string]$Path)
    $trimmed = $Path.TrimEnd('\')
    $cut = $trimmed.LastIndexOf('\')
    if ($cut -lt 1) { return '' }
    return $trimmed.Substring(0, $cut)
}

function Get-GitBashCandidates {
    # Where Git Bash can be, in the order Claude Code looks: its override variable, the
    # standard install folders, then next to the git that is on PATH. `bash` on PATH is
    # never used: C:\Windows\System32\bash.exe is the WSL launcher.
    param([string]$Override, [string]$ProgramFiles, [string]$LocalAppData, [string]$GitOnPath)
    $candidates = @()
    if ($Override) { $candidates += $Override }
    if ($ProgramFiles) { $candidates += (Join-WindowsPath $ProgramFiles 'Git\bin\bash.exe') }
    if ($LocalAppData) { $candidates += (Join-WindowsPath $LocalAppData 'Programs\Git\bin\bash.exe') }
    if ($GitOnPath) {
        $gitRoot = Get-WindowsParent (Get-WindowsParent $GitOnPath)
        if ($gitRoot) { $candidates += (Join-WindowsPath $gitRoot 'bin\bash.exe') }
    }
    $seen = @{}
    $unique = @()
    foreach ($candidate in $candidates) {
        $key = $candidate.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $unique += $candidate
    }
    return $unique
}

function Test-GitBashNeedsOverride {
    # Claude Code finds Git Bash by itself under Program Files and next to the git on
    # PATH. Anywhere else it needs CLAUDE_CODE_GIT_BASH_PATH.
    param([string]$BashPath, [string]$ProgramFiles, [string]$GitOnPath)
    if (-not $BashPath) { return $false }
    if ($ProgramFiles) {
        $standard = Join-WindowsPath $ProgramFiles 'Git\bin\bash.exe'
        if ($BashPath -ieq $standard) { return $false }
    }
    if ($GitOnPath) {
        $gitRoot = Get-WindowsParent (Get-WindowsParent $GitOnPath)
        if ($gitRoot) {
            if ($BashPath -ieq (Join-WindowsPath $gitRoot 'bin\bash.exe')) { return $false }
        }
    }
    return $true
}

function Get-UserPathWithEntry {
    # The user PATH with one folder appended, or $null when the folder is already on it.
    # Existing text is kept exactly as it is (entries may contain %VARIABLES%); entries
    # are only expanded for the comparison.
    param([string]$Current, [string]$Folder)
    $wanted = $Folder.TrimEnd('\')
    foreach ($entry in @($Current -split ';')) {
        $candidate = [System.Environment]::ExpandEnvironmentVariables($entry.Trim()).TrimEnd('\')
        if ($candidate -and ($candidate -ieq $wanted)) { return $null }
    }
    if (-not $Current) { return $Folder }
    if ($Current.EndsWith(';')) { return ($Current + $Folder) }
    return ($Current + ';' + $Folder)
}

function Test-CommandExists {
    param([string]$Name)
    return ($null -ne (Get-Command $Name -ErrorAction SilentlyContinue))
}

# Dot-source guard: `. scripts\prereqs-windows.ps1` loads the functions and stops here.
if ($MyInvocation.InvocationName -eq '.' -or $env:ENTRABOT_SETUP_FUNCTIONS_ONLY -eq '1') { return }

function Write-Step { param([string]$Message) Write-Host ""; Write-Host $Message -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "  [ok] $Message" -ForegroundColor Green }
function Write-Info { param([string]$Message) Write-Host "  $Message" }
function Write-Note { param([string]$Message) Write-Host "  [!!] $Message" -ForegroundColor Yellow }
function Write-Err { param([string]$Message) Write-Host "  [xx] $Message" -ForegroundColor Red }

function Update-SessionPath {
    # Pick up PATH changes made by an installer so the next step in THIS window sees them.
    $machinePath = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machinePath;$userPath"
    $dotnetTools = Join-Path $env:USERPROFILE '.dotnet\tools'
    if ($env:Path -notlike "*$dotnetTools*") { $env:Path = "$env:Path;$dotnetTools" }
}

function Enable-Tls12 {
    # Windows PowerShell 5.1 on older builds still offers TLS 1.0/1.1 first.
    [System.Net.ServicePointManager]::SecurityProtocol = `
        [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
}

function Get-VersionLine {
    # Runs "<program> --version" and returns its first output line, or '' when the
    # program does not start or fails. This is the proof that a tool is usable.
    param([string]$Exe)
    if (-not $Exe) { return '' }
    try {
        # Test-Path also throws for a drive that is not there; inside the try on purpose.
        if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return '' }
        $out = & $Exe '--version'
        if ($LASTEXITCODE -ne 0) { return '' }
        return [string](@($out) | Select-Object -First 1)
    } catch {
        return ''
    }
}

function Get-GitOnPath {
    $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($git) { return [string]$git.Path }
    return ''
}

function Find-GitBash {
    # A Git Bash that actually runs: @{ Path; Version } or $null.
    $override = $env:CLAUDE_CODE_GIT_BASH_PATH
    if (-not $override) { $override = [System.Environment]::GetEnvironmentVariable('CLAUDE_CODE_GIT_BASH_PATH', 'User') }
    $candidates = @(Get-GitBashCandidates -Override $override -ProgramFiles $env:ProgramFiles `
            -LocalAppData $env:LOCALAPPDATA -GitOnPath (Get-GitOnPath))
    foreach ($candidate in $candidates) {
        $version = Get-VersionLine -Exe $candidate
        if ($version) { return @{ Path = $candidate; Version = $version } }
    }
    return $null
}

function Set-GitBashOverride {
    # Tell Claude Code where Git Bash is when it would not find it by itself. Stored for
    # this account (new windows pick it up) and set for this process.
    param([string]$BashPath)
    if (-not (Test-GitBashNeedsOverride -BashPath $BashPath -ProgramFiles $env:ProgramFiles -GitOnPath (Get-GitOnPath))) {
        return
    }
    [System.Environment]::SetEnvironmentVariable('CLAUDE_CODE_GIT_BASH_PATH', $BashPath, 'User')
    $env:CLAUDE_CODE_GIT_BASH_PATH = $BashPath
    Write-Info "Set CLAUDE_CODE_GIT_BASH_PATH for your account so Claude Code finds it."
}

function Get-GitDownloadUrl {
    # The newest release when GitHub answers, otherwise the pinned version.
    param([string]$Kind)
    $architecture = Get-MachineArchitecture -ProcessorArchitecture $env:PROCESSOR_ARCHITECTURE `
        -ProcessorArchiteW6432 $env:PROCESSOR_ARCHITEW6432
    try {
        Enable-Tls12
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' `
            -UseBasicParsing -TimeoutSec 20
        $url = Select-GitAssetUrl -Assets @($release.assets) -Kind $Kind -Architecture $architecture
        if ($url) { return $url }
    } catch {
        Write-Info "(Could not look up the newest Git for Windows release; using version $PinnedGitVersion.)"
    }
    return (Get-PinnedGitUrl -Kind $Kind -Architecture $architecture)
}

function Save-VerifiedDownload {
    # Downloads a program and checks its signature. Returns $true only when the file is
    # there, validly signed, and signed by the expected publisher. A file that fails the
    # check is deleted and never run.
    param([string]$Url, [string]$Destination, [string]$Organization)
    try {
        Enable-Tls12
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
        $signature = Get-AuthenticodeSignature -LiteralPath $Destination
        $subject = ''
        if ($signature.SignerCertificate) { $subject = [string]$signature.SignerCertificate.Subject }
        if (Test-TrustedPublisher -Status ([string]$signature.Status) -Subject $subject -Organization $Organization) {
            return $true
        }
        Write-Note "The download is not validly signed by $Organization (status: $($signature.Status)). It was NOT run."
    } catch {
        Write-Note "Download failed: $($_.Exception.Message)"
    }
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue }
    return $false
}

function New-DownloadFolder {
    $folder = Join-Path $env:TEMP ('entrabot-prereq-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    return $folder
}

function Install-GitWithInstaller {
    # The official Git for Windows installer. Without administrator rights it installs
    # for this account only (into %LOCALAPPDATA%\Programs\Git) and asks for nothing.
    $url = Get-GitDownloadUrl -Kind 'installer'
    if (-not $url) { Write-Note "There is no Git for Windows installer for this kind of processor."; return $false }
    $folder = New-DownloadFolder
    try {
        $installer = Join-Path $folder (Split-Path -Leaf $url)
        Write-Info "Downloading Git for Windows (about 65 MB)..."
        if (-not (Save-VerifiedDownload -Url $url -Destination $installer -Organization $GitPublisher)) { return $false }
        Write-Info "Installing Git for Windows for your account..."
        $installArgs = '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES /NOCANCEL /SP-'
        $process = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            Write-Note "The Git for Windows installer could not run here (exit code $($process.ExitCode)); trying another way."
            return $false
        }
        return $true
    } catch {
        Write-Note "The Git for Windows installer could not run here ($($_.Exception.Message)); trying another way."
        return $false
    } finally {
        if (Test-Path -LiteralPath $folder) { Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Install-PortableGit {
    # Portable Git: the same release as a self-extracting archive. No installer service,
    # no registry; unpacked into the folder a per-user install would use.
    $url = Get-GitDownloadUrl -Kind 'portable'
    if (-not $url) { Write-Note "There is no portable Git for Windows for this kind of processor."; return $false }
    $destination = Join-Path $env:LOCALAPPDATA 'Programs\Git'
    $folder = New-DownloadFolder
    try {
        $archive = Join-Path $folder (Split-Path -Leaf $url)
        Write-Info "Downloading portable Git for Windows (about 60 MB)..."
        if (-not (Save-VerifiedDownload -Url $url -Destination $archive -Organization $GitPublisher)) { return $false }
        Write-Info "Unpacking it to $destination ..."
        $extractArgs = '-o"' + $destination + '" -y'
        $process = Start-Process -FilePath $archive -ArgumentList $extractArgs -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            Write-Note "Unpacking portable Git failed (exit code $($process.ExitCode))."
            return $false
        }
        return $true
    } catch {
        Write-Note "Unpacking portable Git failed: $($_.Exception.Message)"
        return $false
    } finally {
        if (Test-Path -LiteralPath $folder) { Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Add-UserPathEntry {
    # Appends a folder to THIS ACCOUNT's PATH (HKCU\Environment), never the machine's.
    # Returns $true when it was added and $false when it was already there. The value is
    # read unexpanded and written back with the same registry type, so REG_EXPAND_SZ and
    # every existing entry stay exactly as they were.
    param([string]$Folder)
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    if ($null -eq $key) { throw 'The user environment (HKCU\Environment) could not be opened.' }
    try {
        $current = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        if (@($key.GetValueNames()) -contains 'Path') { $kind = $key.GetValueKind('Path') }
        $updated = Get-UserPathWithEntry -Current $current -Folder $Folder
        if ($null -eq $updated) { return $false }
        $key.SetValue('Path', $updated, $kind)
    } finally {
        $key.Close()
    }
    # Removing a variable that does not exist changes nothing, but it makes Windows tell
    # running programs (Explorer, terminals) that the environment changed.
    [System.Environment]::SetEnvironmentVariable('ENTRABOT_PATH_REFRESH', $null, 'User')
    if (-not (@($env:Path -split ';') -contains $Folder)) { $env:Path = "$env:Path;$Folder" }
    return $true
}

function Find-ClaudeCode {
    # A Claude Code that actually runs: @{ Path; Version } or $null.
    $candidates = @()
    $onPath = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { $candidates += [string]$onPath.Path }
    $candidates += (Join-Path $env:USERPROFILE '.local\bin\claude.exe')
    foreach ($candidate in $candidates) {
        $version = Get-VersionLine -Exe $candidate
        if ($version) { return @{ Path = $candidate; Version = $version } }
    }
    return $null
}

function Install-ClaudeCode {
    # The command from Claude Code's own setup page, run in its own PowerShell so this
    # script's strict settings cannot interfere with the vendor's installer.
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $installArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', 'irm https://claude.ai/install.ps1 | iex')
    # When this script was started through cmd.exe or Python from a PowerShell 7 terminal,
    # it inherited PowerShell 7's PSModulePath and would pass it on; Windows PowerShell
    # then cannot load its own in-box modules (about_PSModulePath, "Starting Windows
    # PowerShell from PowerShell 7"). With no PSModulePath the child builds its default.
    $savedModulePath = $env:PSModulePath
    try {
        $env:PSModulePath = $null
        & $shell @installArgs | Out-Host
        return ($LASTEXITCODE -eq 0)
    } catch {
        Write-Note "The Claude Code installer could not be started: $($_.Exception.Message)"
        return $false
    } finally {
        $env:PSModulePath = $savedModulePath
    }
}

function Test-WingetWorks {
    # Present is not the same as usable: judge winget by running it.
    $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) { return $false }
    try {
        $null = & $winget.Path '--version'
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Install-WithWinget {
    # Returns $true when the package is installed afterwards.
    param([string]$Id, [string[]]$Extra = @())
    $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $wingetArgs = @('install', '--id', $Id, '--exact', '--source', 'winget',
        '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity') + $Extra
    & $winget.Path @wingetArgs | Out-Host   # keep installer output off this function's return value
    return (Test-WingetSuccess -ExitCode $LASTEXITCODE)
}

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Strict mode makes reading $LASTEXITCODE an error until some native command has set it.
$global:LASTEXITCODE = 0

$installed = @()
$present = @()
$failed = @()

if ($SkipBuildTools) {
    Write-Note "-SkipBuildTools is no longer needed: build tools are only installed with -WithBuildTools."
}

if (-not ($WithGit -or $WithClaudeCode -or $WithWorkIq -or $WithBuildTools)) {
    Write-Host ""
    Write-Host "Entrabot does not need a separate prerequisites step any more." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  To set up Entrabot, double-click scripts\setup-windows.cmd (or run it from a"
    Write-Host "  console). It installs Python and Entrabot's packages for your account, with no"
    Write-Host "  administrator rights, and the wizard handles the rest."
    Write-Host ""
    Write-Host "  This helper installs optional extras, one switch each:"
    Write-Host "    -WithGit          Git for Windows (Claude Code runs Entrabot's hooks in Git Bash)"
    Write-Host "    -WithClaudeCode   Claude Code"
    Write-Host "    -WithWorkIq       .NET SDK + Microsoft Agent 365 CLI (a365) for Work IQ"
    Write-Host "    -WithBuildTools   Visual Studio Build Tools, C++ (about 6 GB; rarely needed)"
    Write-Host ""
    Write-Host "  Example:"
    Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithGit -WithClaudeCode"
    Write-Host ""
    exit 0
}

$needsWinget = ($WithWorkIq -or $WithBuildTools)
$wingetWorks = $false
if ($needsWinget) {
    $wingetWorks = Test-WingetWorks
    if (-not $wingetWorks) {
        Write-Note "The Windows Package Manager (winget) is not usable for this account, so some extras cannot be installed automatically. Each one below shows where to get it by hand."
    }
}

# ---- Git for Windows ------------------------------------------------------------
# No winget and no administrator rights: the official installer, then the portable
# archive. Success means Git Bash runs, not that an installer returned 0.
if ($WithGit) {
    Write-Step "Git for Windows"
    Update-SessionPath
    $gitBash = Find-GitBash
    $gitHow = 'already installed'
    if (-not $gitBash) {
        if (Install-GitWithInstaller) {
            Update-SessionPath
            $gitBash = Find-GitBash
            $gitHow = 'installed for your account'
        }
    }
    if (-not $gitBash) {
        if (Install-PortableGit) {
            Update-SessionPath
            $gitBash = Find-GitBash
            $gitHow = 'unpacked (portable) for your account'
        }
    }
    if ($gitBash) {
        Set-GitBashOverride -BashPath $gitBash.Path
        Write-Ok "Git for Windows ${gitHow}: $($gitBash.Path) ($($gitBash.Version))"
        if ($gitHow -eq 'already installed') { $present += 'Git for Windows' } else { $installed += 'Git for Windows' }
    } else {
        Write-Err "Git for Windows could not be installed automatically. Get it from https://git-scm.com/downloads/win (it installs without administrator rights), then run this again."
        $failed += 'Git for Windows'
    }
}

# ---- Claude Code ----------------------------------------------------------------
if ($WithClaudeCode) {
    Write-Step "Claude Code"
    Update-SessionPath
    $claude = Find-ClaudeCode
    $claudeHow = 'already installed'
    if (-not $claude) {
        Write-Info "Running the official Claude Code installer (https://claude.ai/install.ps1)..."
        $installerOk = Install-ClaudeCode
        Update-SessionPath
        $claude = Find-ClaudeCode
        $claudeHow = 'installed for your account'
        if (-not $installerOk -and -not $claude) {
            Write-Note "The Claude Code installer reported a problem (see its output above)."
        }
    }
    if ($claude) {
        Write-Ok "Claude Code ${claudeHow}: $($claude.Path) ($($claude.Version))"
        if ($claudeHow -eq 'already installed') { $present += 'Claude Code' } else { $installed += 'Claude Code' }
        # Its installer puts claude.exe in %USERPROFILE%\.local\bin and warns that the
        # folder is not on PATH. Put it on this account's PATH (never the machine's).
        $claudeBin = Join-Path $env:USERPROFILE '.local\bin'
        if (Test-Path -LiteralPath (Join-Path $claudeBin 'claude.exe') -PathType Leaf) {
            try {
                if (Add-UserPathEntry -Folder $claudeBin) {
                    Write-Ok "Added $claudeBin to your account's PATH. New terminal windows will find 'claude'."
                }
            } catch {
                Write-Note "Could not add $claudeBin to your PATH ($($_.Exception.Message)). Start the agent with start-entrabot.cmd, which finds Claude Code there anyway."
            }
        }
    } else {
        Write-Err "Claude Code could not be installed automatically. See https://code.claude.com/docs/en/setup for other ways to install it."
        $failed += 'Claude Code'
    }
}

# ---- Work IQ: .NET SDK + Microsoft Agent 365 CLI ----------------------------------
if ($WithWorkIq) {
    Write-Step ".NET SDK (needed by the Microsoft Agent 365 CLI)"
    Update-SessionPath
    if (Test-CommandExists 'dotnet') {
        Write-Ok ".NET SDK is already installed"
        $present += '.NET SDK'
    } elseif (-not $wingetWorks) {
        Write-Err "Install the .NET SDK 9 from https://dotnet.microsoft.com/download"
        $failed += '.NET SDK'
    } elseif (Install-WithWinget -Id 'Microsoft.DotNet.SDK.9') {
        Update-SessionPath
        Write-Ok ".NET SDK 9 installed"
        $installed += '.NET SDK 9'
    } else {
        Write-Err "The .NET SDK could not be installed. Get it from https://dotnet.microsoft.com/download"
        $failed += '.NET SDK'
    }

    Write-Step "Microsoft Agent 365 CLI (a365)"
    if (-not (Test-CommandExists 'dotnet')) {
        Write-Err "dotnet is not available, so the a365 CLI cannot be installed."
        $failed += 'Agent 365 CLI (a365)'
    } elseif (Test-CommandExists 'a365') {
        Write-Info "a365 is already installed; checking for an update..."
        & dotnet tool update --global Microsoft.Agents.A365.DevTools.Cli
        if ($LASTEXITCODE -ne 0) {
            Write-Note "a365 is installed but could not be updated (exit code $LASTEXITCODE). Continuing with the installed version."
        }
        $present += 'Agent 365 CLI (a365)'
    } else {
        & dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli
        $toolExit = $LASTEXITCODE
        Update-SessionPath
        if ($toolExit -eq 0 -and (Test-CommandExists 'a365')) {
            Write-Ok "a365 installed"
            $installed += 'Agent 365 CLI (a365)'
        } else {
            Write-Err "a365 could not be installed (dotnet tool install exited with $toolExit)."
            $failed += 'Agent 365 CLI (a365)'
        }
    }
}

# ---- Visual Studio Build Tools ----------------------------------------------------
if ($WithBuildTools) {
    Write-Step "Visual Studio Build Tools (C++)"
    $vsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vsWhereArgs = @('-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
        '-property', 'installationPath')
    $hasBuildTools = $false
    if (Test-Path -LiteralPath $vsWhere) {
        $found = & $vsWhere @vsWhereArgs
        if ($found) { $hasBuildTools = $true }
    }
    if ($hasBuildTools) {
        Write-Ok "Visual Studio Build Tools (C++) are already installed"
        $present += 'VS Build Tools'
    } elseif (-not $wingetWorks) {
        Write-Err "Install 'Build Tools for Visual Studio' (C++ workload) from https://visualstudio.microsoft.com/downloads/"
        $failed += 'VS Build Tools'
    } else {
        Write-Info "Installing Visual Studio Build Tools with the C++ workload."
        Write-Info "This takes 5-10 minutes, needs about 6 GB, and asks for administrator approval."
        $override = '--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'
        $ok = Install-WithWinget -Id 'Microsoft.VisualStudio.2022.BuildTools' -Extra @('--override', $override)
        if (-not $ok -and (Test-Path -LiteralPath $vsWhere)) {
            # The Visual Studio installer can report failure after a successful install.
            $found = & $vsWhere @vsWhereArgs
            if ($found) { $ok = $true }
        }
        if ($ok) {
            Write-Ok "Visual Studio Build Tools installed"
            $installed += 'VS Build Tools'
        } else {
            Write-Err "Visual Studio Build Tools could not be installed."
            $failed += 'VS Build Tools'
        }
    }
}

# ---- Summary ----------------------------------------------------------------------
Write-Host ""
Write-Host "Summary" -ForegroundColor Cyan
foreach ($item in $present) { Write-Host "  already installed: $item" -ForegroundColor Green }
foreach ($item in $installed) { Write-Host "  newly installed:   $item" -ForegroundColor Green }
foreach ($item in $failed) { Write-Host "  FAILED:            $item" -ForegroundColor Red }
Write-Host ""

if ($failed.Count -gt 0) {
    Write-Err "Some extras could not be installed. See the messages above."
    exit 1
}
Write-Ok "Done."
exit 0
