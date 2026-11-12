<#
.SYNOPSIS
  Entrabot - Windows setup. Start here (or double-click scripts\setup-windows.cmd).

.DESCRIPTION
  This script is the BOOTSTRAP. It gets a brand-new Windows PC to the point where the
  real setup wizard can run, then hands over to it:

    1. Checks this PC (Windows, not WSL, not somebody else's administrator account).
    2. Finds Python 3.12 or newer. If there is none it downloads a private copy of
       Python just for Entrabot (the official nuget.org package, checked against a
       pinned SHA-512 checksum and the Python Software Foundation's signature; nothing
       else on the PC is changed) - no administrator rights, no installer, nothing to
       restart.
    3. Creates the project's private Python environment (.venv) and installs Entrabot.
       Later runs skip this when nothing has changed.
    4. Starts the setup wizard:  python -m entrabot.winsetup

  The wizard does everything else: signing in to Azure, creating or joining the agent,
  the certificate, memory (local or cloud), registering the MCP server, and a final
  check that the agent can sign in.

  Runs on the Windows PowerShell 5.1 that ships with Windows, and on PowerShell 7.
  Nothing here needs PowerShell 7, winget, git, or administrator rights.

  With no parameters the wizard asks its questions interactively. Every question can
  also be answered up front with a parameter, and -Yes makes the run unattended.

.PARAMETER NewChain
  Create a brand-new agent (Blueprint, Agent Identity and Agent User).

.PARAMETER UseBlueprint
  Join an existing agent Blueprint by its App ID.

.PARAMETER UpnSuffix
  Suffix for the Agent User's sign-in name, for example "bob".

.PARAMETER AgentUserUpn
  Use this exact Agent User sign-in name, for example
  entrabot-agent-bob@yourtenant.onmicrosoft.com.

.PARAMETER Sponsor
  Who the agent takes instructions from in Teams (sign-in name). Repeat or separate
  with commas for more than one. Default: the account that signs in during setup.

.PARAMETER UseCloudMemory
  Keep the agent's memory in Azure Blob Storage.

.PARAMETER KeepMemoryLocal
  Keep the agent's memory on this PC.

.PARAMETER WithStorageAccount
  Cloud memory: use this Storage Account (created if missing).

.PARAMETER WithContainer
  Cloud memory: use this blob container.

.PARAMETER CreateNewStorage
  Cloud memory: always create a fresh Storage Account.

.PARAMETER Location
  Cloud memory: Azure region for a new Storage Account, for example westus2.

.PARAMETER Subscription
  Cloud memory: Azure subscription name or ID to use.

.PARAMETER ConfigureA365WorkIq
  Also set up Microsoft Agent 365 Work IQ (needs the a365 CLI; see
  scripts\prereqs-windows.ps1 -WithWorkIq).

.PARAMETER Tenant
  Sign in to this tenant (domain or tenant ID).

.PARAMETER DeviceCode
  Sign in with a device code instead of a browser window.

.PARAMETER Hosts
  Which AI hosts to register the MCP server for: claude, copilot, or claude,copilot.

.PARAMETER SkipSmoke
  Skip the final sign-in check.

.PARAMETER Yes
  Unattended: never ask a question. Anything that still needs an answer is an error.

.PARAMETER Diagnose
  Do not change anything; check an existing setup and explain what is wrong. Never
  installs, repairs or moves anything: if this folder is not set up yet it says so.

.PARAMETER Verbose
  Show everything: the full package-install output here, and the wizard's full helper
  output instead of its summaries.

.PARAMETER BootstrapOnly
  Stop once Python and .venv are ready, without starting the wizard (used by CI).

.PARAMETER Status
  Show the agent's status (delegates to status-windows.ps1).

.PARAMETER Json
  With -Status, output machine-readable JSON.

.PARAMETER HealthOnly
  With -Status, only print health checks.

.PARAMETER Strict
  With -Status, return non-zero when health checks fail.

.PARAMETER A365AgentName
  Deprecated and ignored. Work IQ setup uses the existing Entrabot Blueprint.

.PARAMETER Migrate
  Deprecated and ignored. Old data is moved automatically.

.EXAMPLE
  scripts\setup-windows.cmd

  The interactive wizard. This is all most people need.

.EXAMPLE
  scripts\setup-windows.cmd -NewChain -UpnSuffix bob -KeepMemoryLocal -Yes

.EXAMPLE
  scripts\setup-windows.cmd -UseBlueprint 11111111-2222-3333-4444-555555555555 `
      -UseCloudMemory -WithStorageAccount mycorpstg -WithContainer bob-mem

.EXAMPLE
  scripts\setup-windows.cmd -Diagnose
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$NewChain,
    [string]$UseBlueprint = "",
    [string]$UpnSuffix = "",
    [string]$AgentUserUpn = "",
    [string[]]$Sponsor = @(),
    [switch]$UseCloudMemory,
    [switch]$KeepMemoryLocal,
    [string]$WithStorageAccount = "",
    [string]$WithContainer = "",
    [switch]$CreateNewStorage,
    [string]$Location = "",
    [string]$Subscription = "",
    [switch]$ConfigureA365WorkIq,
    [string]$A365AgentName = "",
    [string]$Tenant = "",
    [switch]$DeviceCode,
    [string]$Hosts = "",
    [switch]$SkipSmoke,
    [switch]$Yes,
    [switch]$Diagnose,
    [switch]$BootstrapOnly,
    [switch]$Migrate,
    [switch]$Status,
    [switch]$Json,
    [switch]$HealthOnly,
    [switch]$Strict,
    [switch]$Help
)

# Windows PowerShell 5.1 writes progress records as CLIXML to stderr when its output is
# not a console (SSH, CI, a captured run), and its progress bar makes downloads many
# times slower. Nothing here needs a progress bar.
$ProgressPreference = 'SilentlyContinue'

# =============================================================================
# Pure helpers. Everything above the dot-source guard is side-effect free so the
# tests can load this file and call these functions under PowerShell 5.1 and 7.
# This file must stay ASCII-only: Windows PowerShell 5.1 reads a file without a
# byte-order mark in the ANSI code page, where one non-ASCII character is a
# parse error.
# =============================================================================

$PythonInstallerVersion = '3.12.10'   # last 3.12 release with a Windows installer

function ConvertTo-PythonVersion {
    # '3.12.10', 'Python 3.12.10' -> [version]; anything else -> $null.
    param([string]$Text)
    if (-not $Text) { return $null }
    $found = [regex]::Match($Text, '(?<![\d.])(\d+)\.(\d+)\.(\d+)(?![\d])')
    if (-not $found.Success) { return $null }
    return [version]$found.Value
}

function Test-PythonVersionOk {
    param([string]$Text)
    $version = ConvertTo-PythonVersion -Text $Text
    if ($null -eq $version) { return $false }
    return ($version -ge [version]'3.12')
}

function Test-StoreAliasPath {
    # Anything under a WindowsApps folder is the Microsoft Store alias: either a stub
    # that only opens the Store, or the Store build of Python, which redirects writes
    # under %LOCALAPPDATA% into its own package folder. Entrabot keeps its data in
    # %LOCALAPPDATA%\entrabot, so neither is usable.
    param([string]$Path)
    if (-not $Path) { return $false }
    return ($Path -match '(?i)[\\/]WindowsApps[\\/]')
}

function Get-PyLauncherPaths {
    # Interpreter paths from the output of `py -0p`.
    param([string[]]$Lines)
    $paths = @()
    foreach ($line in @($Lines)) {
        if (-not $line) { continue }
        $found = [regex]::Match($line, '([A-Za-z]:\\.*\.exe)\s*$')
        if ($found.Success) { $paths += $found.Groups[1].Value }
    }
    return $paths
}

function Select-PreferredPython {
    # Candidates: objects with Path and Version. Picks 3.12 (what CI tests), then 3.13,
    # then the newest remaining release. Too-old and Store interpreters are never used.
    param([object[]]$Candidates)
    $usable = @()
    foreach ($candidate in @($Candidates)) {
        if ($null -eq $candidate) { continue }
        $path = [string]$candidate.Path
        if (-not $path) { continue }
        if (Test-StoreAliasPath -Path $path) { continue }
        $version = ConvertTo-PythonVersion -Text ([string]$candidate.Version)
        if ($null -eq $version) { continue }
        if ($version -lt [version]'3.12') { continue }
        $usable += @{ Path = $path; Version = $version }
    }
    foreach ($minor in @(12, 13)) {
        $hits = @($usable | Where-Object { $_.Version.Major -eq 3 -and $_.Version.Minor -eq $minor } |
            Sort-Object -Property { $_.Version } -Descending)
        if ($hits.Count -gt 0) { return $hits[0].Path }
    }
    $rest = @($usable | Sort-Object -Property { $_.Version } -Descending)
    if ($rest.Count -gt 0) { return $rest[0].Path }
    return $null
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

function Get-PythonInstallerUrl {
    param([string]$Architecture)
    if ($Architecture -ne 'amd64' -and $Architecture -ne 'arm64') { return $null }
    return ('https://www.python.org/ftp/python/{0}/python-{0}-{1}.exe' -f $PythonInstallerVersion, $Architecture)
}

# SHA-512 of each official Python package on nuget.org, base64: the form nuget.org
# publishes (PackageHash, with PackageHashAlgorithm SHA512). Pinned on 2026-11-11 after
# two sources agreed exactly: the hash of the package downloaded from
#   https://www.nuget.org/api/v2/package/<id>/3.12.10
# and the PackageHash nuget.org publishes for that exact version at
#   https://www.nuget.org/api/v2/Packages(Id='<id>',Version='3.12.10')
# Keyed by id AND version, so changing $PythonInstallerVersion without re-pinning (both
# sources again) turns the private-copy route off instead of trusting an unpinned file.
$PythonNuGetSha512 = @{
    'python/3.12.10'      = 'u9pNz2iKlCEbYtUJaKkbOPMF0LjR7NkCafdKhvigpPzrt8oWKgdTpHaR6z3wyWQAm9PYGUxv0Zr66NX9AeHMDw=='
    'pythonarm64/3.12.10' = 's3YP8TRRHOfg/JPmc96KP1bLPIlOjgt+MsJLyocX+BJ0qy9RmwHwogDZLnp9LsArUCmpJPd1LP+aUIXbX2kW2w=='
}

function Get-PythonNuGetPackageId {
    param([string]$Architecture)
    if ($Architecture -eq 'amd64') { return 'python' }
    if ($Architecture -eq 'arm64') { return 'pythonarm64' }
    return $null
}

function Get-PythonNuGetUrl {
    # The official Python packages on nuget.org (published by the Python Software
    # Foundation): a plain zip of a complete interpreter. No installer, no registry.
    param([string]$Architecture)
    $packageId = Get-PythonNuGetPackageId -Architecture $Architecture
    if (-not $packageId) { return $null }
    return ('https://www.nuget.org/api/v2/package/{0}/{1}' -f $packageId, $PythonInstallerVersion)
}

function Get-PythonNuGetSha512 {
    # The pinned SHA-512 (base64) of the package for this processor, or $null when there
    # is none, in which case the private copy is not offered at all.
    param([string]$Architecture)
    $packageId = Get-PythonNuGetPackageId -Architecture $Architecture
    if (-not $packageId) { return $null }
    $key = '{0}/{1}' -f $packageId, $PythonInstallerVersion
    if (-not $PythonNuGetSha512.ContainsKey($key)) { return $null }
    return [string]$PythonNuGetSha512[$key]
}

function Test-PackageHashMatch {
    # Get-FileHash reports hex; nuget.org publishes base64. Compares the bytes exactly.
    param([string]$Sha512Hex, [string]$ExpectedBase64)
    if (-not $ExpectedBase64) { return $false }
    if ($Sha512Hex -notmatch '^[0-9A-Fa-f]{128}$') { return $false }
    $bytes = New-Object byte[] 64
    for ($index = 0; $index -lt 64; $index++) {
        $bytes[$index] = [System.Convert]::ToByte($Sha512Hex.Substring($index * 2, 2), 16)
    }
    return ([System.Convert]::ToBase64String($bytes) -ceq $ExpectedBase64)
}

function Test-InstallerUnavailable {
    # 1601: the Windows Installer service cannot be reached (seen for a standard user in
    # a remote session). 1603: fatal error during installation. Either way the MSI-based
    # installer is not an option in this session; it says nothing about the PC being broken.
    param([int]$ExitCode)
    return (@(1601, 1603) -contains $ExitCode)
}

function Get-PythonRouteMessage {
    # One line telling the user where the Python that setup uses came from.
    param([string]$Route, [string]$Version)
    switch ($Route) {
        'venv' { return "Using this folder's existing Python environment (Python $Version)" }
        'existing' { return "Using Python $Version already on this PC" }
        'private' { return "Using Entrabot's private copy of Python $Version - nothing else on this PC is changed" }
        'private-new' { return "Downloaded a private copy of Python $Version for Entrabot - nothing else on this PC is changed" }
        'winget' { return "Installed Python $Version for your account with the Windows Package Manager" }
        'pythonorg' { return "Installed Python $Version for your account with the python.org installer" }
        default { return "Using Python $Version" }
    }
}

function Get-LegacyDataAction {
    # What to do about data in the old location (%USERPROFILE%\.entrabot):
    #   none     - there is none
    #   move     - move it to the current location (%LOCALAPPDATA%\entrabot)
    #   conflict - both locations hold data; never merge or delete automatically
    param([bool]$LegacyHasContent, [bool]$CurrentHasContent)
    if (-not $LegacyHasContent) { return 'none' }
    if ($CurrentHasContent) { return 'conflict' }
    return 'move'
}

function ConvertFrom-DistinguishedName {
    # Splits a certificate subject such as  CN=Name, O="Org, Inc.", C=US  into its
    # attribute=value parts the way .NET writes it: parts are separated by , ; or + outside
    # quotes, a value may be "quoted" ("" is a quote inside one), and \ escapes the next
    # character. Returns an array of @{ Key; Value }, or $null when the text is not a
    # well-formed name (a subject that cannot be read is never trusted).
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

function Test-SubjectHasExactly {
    # True only when the subject parses, has exactly ONE $Attribute part, and its value is
    # exactly $Expected (case-sensitive). The organization can also be written as its
    # object identifier, so those spellings count as O as well.
    param([string]$Subject, [string]$Attribute, [string]$Expected)
    if (-not $Expected) { return $false }
    $parts = ConvertFrom-DistinguishedName -Subject $Subject
    if ($null -eq $parts) { return $false }
    $names = @($Attribute)
    if ($Attribute -eq 'O') { $names = @('O', 'OID.2.5.4.10', '2.5.4.10') }
    if ($Attribute -eq 'CN') { $names = @('CN', 'OID.2.5.4.3', '2.5.4.3') }
    $values = @()
    foreach ($part in $parts) {
        if ($names -contains $part.Key) { $values += [string]$part.Value }
    }
    if ($values.Count -ne 1) { return $false }
    return ($values[0] -ceq $Expected)
}

function Test-TrustedPythonSignature {
    # The downloaded installer is only run when Windows says its signature is valid AND
    # the signer's organization (O=) is exactly the Python Software Foundation. The
    # subject is parsed, not searched: a quoted CN can contain the text ", O=...,".
    param([string]$Status, [string]$Subject)
    if ($Status -ne 'Valid') { return $false }
    return (Test-SubjectHasExactly -Subject $Subject -Attribute 'O' -Expected 'Python Software Foundation')
}

function Test-DifferentAccount {
    # True only for "elevated with another account's credentials on someone's desktop".
    # Everything Entrabot creates (certificate, credentials, .env permissions) belongs to
    # the account that runs setup, so that person's agent would be unusable by the person
    # actually logged on. A plain remote session has no desktop owner and never matches.
    param([string]$CurrentSid, [string]$SessionOwnerSid, [bool]$Elevated)
    if (-not $Elevated) { return $false }
    if (-not $CurrentSid -or -not $SessionOwnerSid) { return $false }
    return ($CurrentSid -ne $SessionOwnerSid)
}

function Test-LongPathRisk {
    # Windows limits paths to 260 characters unless LongPathsEnabled is 1 (changing it
    # needs an administrator, so setup never does). Installed packages nest about 170
    # characters below the project folder.
    param([string]$ProjectRoot, [int]$LongPathsEnabled)
    if ($LongPathsEnabled -eq 1) { return $false }
    if (-not $ProjectRoot) { return $false }
    return ($ProjectRoot.Length -gt 90)
}

function Test-CompilerNeeded {
    # Does pip's output say a C compiler was required?
    param([string[]]$Lines)
    foreach ($line in @($Lines)) {
        if (-not $line) { continue }
        if ($line -match 'Microsoft Visual C\+\+ \d+(\.\d+)? or greater is required') { return $true }
        if ($line -match "cl\.exe'? failed") { return $true }
        if ($line -match 'Unable to find vcvarsall') { return $true }
    }
    return $false
}

function ConvertTo-NativeArgument {
    # One argument, quoted the way Windows programs split their command line (the
    # CommandLineToArgvW rules): quote when there is whitespace, escape embedded quotes,
    # and double any backslashes that end up in front of a quote. Start-Process on
    # Windows PowerShell 5.1 joins its arguments with spaces and quotes nothing.
    param([string]$Value)
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes++
            continue
        }
        if ($character -eq [char]34) {
            [void]$builder.Append([char]92, ($backslashes * 2 + 1))
        } else {
            [void]$builder.Append([char]92, $backslashes)
        }
        [void]$builder.Append($character)
        $backslashes = 0
    }
    [void]$builder.Append([char]92, ($backslashes * 2))
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Join-NativeArguments {
    param([string[]]$Arguments)
    $quoted = @()
    foreach ($argument in @($Arguments)) { $quoted += (ConvertTo-NativeArgument -Value $argument) }
    return ($quoted -join ' ')
}

function Get-InstallStamp {
    # Identifies "what was installed into .venv": the project definition (a hash of
    # pyproject.toml) and the extras. Stored in .venv after a successful install.
    param([string]$PyprojectHash, [string]$Extras)
    return ('{0}|{1}' -f $PyprojectHash.ToLowerInvariant(), $Extras)
}

function Test-InstallCurrent {
    param([string]$Expected, [string]$Recorded)
    if (-not $Expected -or -not $Recorded) { return $false }
    return ($Expected -eq $Recorded.Trim())
}

function Test-BoundSwitch {
    param([hashtable]$Bound, [string]$Name)
    if (-not $Bound.ContainsKey($Name)) { return $false }
    return [bool]$Bound[$Name]
}

function Get-BoundText {
    param([hashtable]$Bound, [string]$Name)
    if (-not $Bound.ContainsKey($Name)) { return '' }
    if ($null -eq $Bound[$Name]) { return '' }
    return ([string]$Bound[$Name]).Trim()
}

function Get-ParameterProblems {
    # Contradictions that can be reported before anything is downloaded or installed.
    param([hashtable]$Bound)
    $problems = @()
    if ((Test-BoundSwitch $Bound 'NewChain') -and (Get-BoundText $Bound 'UseBlueprint')) {
        $problems += '-NewChain and -UseBlueprint are mutually exclusive: create a new agent, or join an existing one.'
    }
    if ((Test-BoundSwitch $Bound 'UseCloudMemory') -and (Test-BoundSwitch $Bound 'KeepMemoryLocal')) {
        $problems += '-UseCloudMemory and -KeepMemoryLocal are mutually exclusive.'
    }
    if ((Test-BoundSwitch $Bound 'CreateNewStorage') -and (Get-BoundText $Bound 'WithStorageAccount')) {
        $problems += '-CreateNewStorage and -WithStorageAccount are mutually exclusive.'
    }
    return $problems
}

function Get-WizardArguments {
    # The argument list handed to `python -m entrabot.winsetup`, built from the
    # parameters the caller actually passed. Values are separate array elements, so
    # nothing is ever quoted by hand.
    param([hashtable]$Bound, [string]$ProjectRoot)

    if (Test-BoundSwitch $Bound 'Diagnose') {
        return @('diagnose', '--project-root', $ProjectRoot)
    }

    $list = New-Object 'System.Collections.Generic.List[string]'
    $list.Add('setup')
    $list.Add('--project-root')
    $list.Add($ProjectRoot)

    if (Test-BoundSwitch $Bound 'NewChain') { $list.Add('--new-chain') }
    $valueFlags = @(
        @('UseBlueprint', '--use-blueprint'),
        @('UpnSuffix', '--upn-suffix'),
        @('AgentUserUpn', '--agent-user-upn')
    )
    foreach ($pair in $valueFlags) {
        $value = Get-BoundText $Bound $pair[0]
        if ($value) { $list.Add($pair[1]); $list.Add($value) }
    }

    if ($Bound.ContainsKey('Sponsor') -and $null -ne $Bound['Sponsor']) {
        foreach ($entry in @($Bound['Sponsor'])) {
            foreach ($piece in ([string]$entry).Split(',')) {
                $sponsorName = $piece.Trim()
                if ($sponsorName) { $list.Add('--sponsor'); $list.Add($sponsorName) }
            }
        }
    }

    if (Test-BoundSwitch $Bound 'UseCloudMemory') { $list.Add('--memory'); $list.Add('cloud') }
    if (Test-BoundSwitch $Bound 'KeepMemoryLocal') { $list.Add('--memory'); $list.Add('local') }

    $storageFlags = @(
        @('WithStorageAccount', '--storage-account'),
        @('WithContainer', '--container')
    )
    foreach ($pair in $storageFlags) {
        $value = Get-BoundText $Bound $pair[0]
        if ($value) { $list.Add($pair[1]); $list.Add($value) }
    }
    if (Test-BoundSwitch $Bound 'CreateNewStorage') { $list.Add('--create-new-storage') }

    $placementFlags = @(
        @('Location', '--location'),
        @('Subscription', '--subscription')
    )
    foreach ($pair in $placementFlags) {
        $value = Get-BoundText $Bound $pair[0]
        if ($value) { $list.Add($pair[1]); $list.Add($value) }
    }

    if (Test-BoundSwitch $Bound 'ConfigureA365WorkIq') { $list.Add('--configure-a365-work-iq') }
    $tenantValue = Get-BoundText $Bound 'Tenant'
    if ($tenantValue) { $list.Add('--tenant'); $list.Add($tenantValue) }
    if (Test-BoundSwitch $Bound 'DeviceCode') { $list.Add('--device-code') }
    $hostsValue = Get-BoundText $Bound 'Hosts'
    if ($hostsValue) { $list.Add('--hosts'); $list.Add($hostsValue) }
    if (Test-BoundSwitch $Bound 'SkipSmoke') { $list.Add('--skip-smoke') }
    if (Test-BoundSwitch $Bound 'Yes') { $list.Add('--yes') }
    if (Test-BoundSwitch $Bound 'Verbose') { $list.Add('--verbose') }

    return $list.ToArray()
}

# -----------------------------------------------------------------------------
# Dot-source guard: `. scripts\setup-windows.ps1` loads the functions above and
# stops here. Nothing below runs unless the script is actually executed.
# -----------------------------------------------------------------------------
if ($MyInvocation.InvocationName -eq '.' -or $env:ENTRABOT_SETUP_FUNCTIONS_ONLY -eq '1') { return }

# =============================================================================
# Output helpers (plain ASCII; the Python wizard owns the real user interface)
# =============================================================================

function Write-Step { param([string]$Message) Write-Host ""; Write-Host $Message -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "  [ok] $Message" -ForegroundColor Green }
function Write-Info { param([string]$Message) Write-Host "  $Message" }
function Write-Note { param([string]$Message) Write-Host "  [!!] $Message" -ForegroundColor Yellow }
function Stop-Setup {
    # Print what went wrong and how to fix it, then exit.
    param([string]$Message, [string[]]$Fix = @(), [int]$Code = 1)
    Write-Host ""
    Write-Host "  [xx] $Message" -ForegroundColor Red
    foreach ($line in $Fix) { Write-Host "       $line" }
    Write-Host ""
    exit $Code
}

# =============================================================================
# Machine helpers (touch the system; only called from the main body)
# =============================================================================

function Get-CurrentSid {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

function Test-Elevated {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-SessionOwnerSid {
    # The account whose desktop this window is on: the owner of explorer.exe in this
    # logon session. Empty when there is no desktop (SSH, scheduled task, service).
    # WMI can refuse the query ("Access denied" for a standard user in a remote
    # session). Not being able to tell must never block setup or print an error.
    try {
        $sessionId = (Get-Process -Id $PID -ErrorAction Stop).SessionId
        $shells = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop `
                -Filter "Name = 'explorer.exe' AND SessionId = $sessionId")
        foreach ($shell in $shells) {
            $owner = Invoke-CimMethod -InputObject $shell -MethodName GetOwnerSid -ErrorAction Stop
            if ($null -ne $owner -and $owner.Sid) { return [string]$owner.Sid }
        }
    } catch {
        return ''
    }
    return ''
}

function Get-LongPathsEnabled {
    try {
        $key = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
        if ($key.PSObject.Properties['LongPathsEnabled']) { return [int]$key.LongPathsEnabled }
    } catch {
        return 0
    }
    return 0
}

function Get-PythonVersionText {
    # Runs the interpreter and returns '3.12.10', or '' when it does not start.
    param([string]$Exe)
    if (-not $Exe) { return '' }
    try {
        # Test-Path throws for a drive that is not there (a stale registry or PATH entry
        # pointing at an unplugged disk); inside the try on purpose.
        if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return '' }
        $out = & $Exe '-c' 'import sys; print(''%d.%d.%d'' % sys.version_info[:3])'
        if ($LASTEXITCODE -ne 0) { return '' }
        return [string](@($out) | Select-Object -Last 1)
    } catch {
        return ''
    }
}

function Get-PythonCandidatePaths {
    $paths = New-Object 'System.Collections.Generic.List[string]'

    # 1. The py launcher knows every registered interpreter.
    $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) {
        try {
            $listing = & $launcher.Path '-0p'
            foreach ($path in @(Get-PyLauncherPaths -Lines @($listing))) { $paths.Add($path) }
        } catch {
            Write-Info "(The py launcher could not list interpreters: $($_.Exception.Message))"
        }
    }

    # 2. PEP 514 registry entries: per-user installs first, then all-users installs.
    foreach ($hive in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore')) {
        if (-not (Test-Path -LiteralPath $hive)) { continue }
        foreach ($tag in @(Get-ChildItem -LiteralPath $hive -ErrorAction SilentlyContinue)) {
            $installKey = Join-Path $tag.PSPath 'InstallPath'
            if (-not (Test-Path -LiteralPath $installKey)) { continue }
            $props = Get-ItemProperty -LiteralPath $installKey
            $exe = ''
            if ($props.PSObject.Properties['ExecutablePath']) { $exe = [string]$props.ExecutablePath }
            if (-not $exe -and $props.PSObject.Properties['(default)']) {
                # String join on purpose: Join-Path throws "Cannot find drive" when a
                # stale registry entry points at a drive that is no longer there.
                $exe = ([string]$props.'(default)').TrimEnd('\') + '\python.exe'
            }
            if ($exe) { $paths.Add($exe) }
        }
    }

    # 3. The default per-user install folder, in case the registry entry is missing.
    if ($env:LOCALAPPDATA) {
        $programs = Join-Path $env:LOCALAPPDATA 'Programs\Python'
        if (Test-Path -LiteralPath $programs) {
            foreach ($dir in @(Get-ChildItem -LiteralPath $programs -Directory -ErrorAction SilentlyContinue)) {
                $paths.Add((Join-Path $dir.FullName 'python.exe'))
            }
        }
    }

    # 4. Whatever `python` is on PATH.
    foreach ($command in @(Get-Command python -All -CommandType Application -ErrorAction SilentlyContinue)) {
        if ($command.Path) { $paths.Add($command.Path) }
    }

    $seen = @{}
    $unique = @()
    foreach ($path in $paths) {
        $key = $path.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $unique += $path
    }
    return $unique
}

function Find-Python {
    # The best real interpreter on this PC, or $null.
    $candidates = @()
    foreach ($path in @(Get-PythonCandidatePaths)) {
        if (Test-StoreAliasPath -Path $path) { continue }
        $version = Get-PythonVersionText -Exe $path
        if (-not $version) { continue }
        $candidates += @{ Path = $path; Version = $version }
    }
    return (Select-PreferredPython -Candidates $candidates)
}

function Test-WingetWorks {
    # winget can be present but unusable (never registered for this account, blocked by
    # policy), so it is judged by running it, not by finding it.
    $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) { return $false }
    try {
        $null = & $winget.Path '--version'
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Install-PythonWithWinget {
    $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) { return $false }
    $wingetArgs = @(
        'install', '--id', 'Python.Python.3.12', '--exact', '--source', 'winget',
        '--scope', 'user', '--silent',
        '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'
    )
    try {
        # Out-Host keeps winget's output on screen and off this function's return value.
        & $winget.Path @wingetArgs | Out-Host
        $code = $LASTEXITCODE
    } catch {
        Write-Note "winget could not be started: $($_.Exception.Message)"
        return $false
    }
    # 0 = installed; the others mean "already there / nothing newer".
    if (@(0, -1978335189, -1978335135, -1978335153, -1978334963) -contains $code) { return $true }
    Write-Note "winget could not install Python (exit code $code)."
    return $false
}

function Enable-Tls12 {
    # Windows PowerShell 5.1 on older builds still offers TLS 1.0/1.1 first.
    [System.Net.ServicePointManager]::SecurityProtocol = `
        [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
}

function Test-PrivatePythonTrusted {
    # python.exe and the python3*.dll files beside it must each carry a valid signature
    # from the Python Software Foundation. Checked before the copy is ever run.
    param([string]$ToolsDir)
    $files = @((Join-Path $ToolsDir 'python.exe'))
    foreach ($dll in @(Get-ChildItem -LiteralPath $ToolsDir -Filter 'python3*.dll' -File -ErrorAction SilentlyContinue)) {
        $files += $dll.FullName
    }
    foreach ($file in $files) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            Write-Note "Expected file is missing from the Python package: $file"
            return $false
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $file
        $subject = ''
        if ($signature.SignerCertificate) { $subject = [string]$signature.SignerCertificate.Subject }
        if (-not (Test-TrustedPythonSignature -Status ([string]$signature.Status) -Subject $subject)) {
            Write-Note "$(Split-Path -Leaf $file) is not validly signed by the Python Software Foundation (status: $($signature.Status))."
            return $false
        }
    }
    return $true
}

function Get-PrivatePython {
    # Entrabot's own copy of Python under <data folder>\python\<version>, unpacked from
    # the official NuGet package. Returns @{ Path; Downloaded } or $null. A new download
    # must match the package's pinned SHA-512 before it is unpacked, and its signed files
    # must pass Authenticode in a staging folder before it is moved into place. An
    # existing copy is reused after its signed files pass Authenticode again (the package
    # checksum cannot be re-checked once unpacked). This function never runs the interpreter.
    param([string]$DataRoot)
    $pythonRoot = Join-Path $DataRoot 'python'
    $copyDir = Join-Path $pythonRoot $PythonInstallerVersion
    $toolsDir = Join-Path $copyDir 'tools'
    $exe = Join-Path $toolsDir 'python.exe'

    if (Test-Path -LiteralPath $exe -PathType Leaf) {
        if (Test-PrivatePythonTrusted -ToolsDir $toolsDir) {
            return @{ Path = $exe; Downloaded = $false }
        }
        Write-Note "The private copy of Python at $copyDir failed its signature check; replacing it."
        Remove-Item -LiteralPath $copyDir -Recurse -Force
    }

    $architecture = Get-MachineArchitecture -ProcessorArchitecture $env:PROCESSOR_ARCHITECTURE `
        -ProcessorArchiteW6432 $env:PROCESSOR_ARCHITEW6432
    $url = Get-PythonNuGetUrl -Architecture $architecture
    $expectedHash = Get-PythonNuGetSha512 -Architecture $architecture
    if (-not $url -or -not $expectedHash) {
        Write-Note "There is no pinned 64-bit Python package for this kind of processor ($architecture)."
        return $null
    }
    $packageId = Get-PythonNuGetPackageId -Architecture $architecture

    $token = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $staging = Join-Path $pythonRoot ('.new-' + $token)
    $stagedTools = Join-Path $staging 'tools'
    $package = Join-Path $env:TEMP ('entrabot-python-' + $token + '.zip')
    try {
        Write-Info "Python 3.12 or newer was not found on this PC."
        Write-Info "Downloading a private copy of Python $PythonInstallerVersion for Entrabot (about 14 MB)..."
        Enable-Tls12
        Invoke-WebRequest -Uri $url -OutFile $package -UseBasicParsing
        # The whole package is pinned before anything in it is unpacked. Authenticode
        # (below) covers python.exe and python3*.dll only; vcruntime140*.dll, DLLs\*.pyd
        # and Lib\ carry no signature, so only this checksum vouches for them.
        $actualHash = (Get-FileHash -LiteralPath $package -Algorithm SHA512).Hash
        if (-not (Test-PackageHashMatch -Sha512Hex $actualHash -ExpectedBase64 $expectedHash)) {
            Remove-Item -LiteralPath $package -Force
            Write-Note "The downloaded Python package does not match the SHA-512 checksum pinned for $packageId $PythonInstallerVersion. It was deleted and NOT used."
            return $null
        }
        Write-Ok "Package checksum verified (SHA-512 pinned for nuget.org $packageId $PythonInstallerVersion)"
        New-Item -ItemType Directory -Path $staging -Force | Out-Null
        Expand-Archive -LiteralPath $package -DestinationPath $staging -Force
        if (-not (Test-PrivatePythonTrusted -ToolsDir $stagedTools)) {
            Write-Note "python.exe or python3*.dll in the downloaded package is not signed by the Python Software Foundation. It was NOT used."
            return $null
        }
        Write-Ok "python.exe and python3*.dll are signed by the Python Software Foundation"
        if (Test-Path -LiteralPath $copyDir) { Remove-Item -LiteralPath $copyDir -Recurse -Force }
        Rename-Item -LiteralPath $staging -NewName $PythonInstallerVersion
        return @{ Path = $exe; Downloaded = $true }
    } catch {
        Write-Note "Could not download a private copy of Python: $($_.Exception.Message)"
        return $null
    } finally {
        if (Test-Path -LiteralPath $package) { Remove-Item -LiteralPath $package -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Move-LegacyData {
    # PowerShell twin of entrabot.config.migrate_legacy_data_dir, for the one case where
    # setup has to write under the data folder before any Python exists to run the real
    # helper. Only called when the current folder has no content.
    param([string]$Legacy, [string]$Current)
    if (Test-Path -LiteralPath $Current) { Remove-Item -LiteralPath $Current -Force }
    $parent = Split-Path -Parent $Current
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Move-Item -LiteralPath $Legacy -Destination $Current
}

function Install-PythonFromPythonOrg {
    # Download the official installer, check its signature, install for this account only.
    $architecture = Get-MachineArchitecture -ProcessorArchitecture $env:PROCESSOR_ARCHITECTURE `
        -ProcessorArchiteW6432 $env:PROCESSOR_ARCHITEW6432
    $url = Get-PythonInstallerUrl -Architecture $architecture
    if (-not $url) {
        Write-Note "There is no 64-bit Python installer for this kind of processor ($architecture)."
        return $false
    }

    $workDir = Join-Path $env:TEMP ('entrabot-python-' + [guid]::NewGuid().ToString('N'))
    $installer = Join-Path $workDir (Split-Path -Leaf $url)
    try {
        New-Item -ItemType Directory -Path $workDir -Force | Out-Null
        Write-Info "Downloading the Python $PythonInstallerVersion installer from python.org (about 27 MB)..."
        Enable-Tls12
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing

        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        $subject = ''
        if ($signature.SignerCertificate) { $subject = [string]$signature.SignerCertificate.Subject }
        if (-not (Test-TrustedPythonSignature -Status ([string]$signature.Status) -Subject $subject)) {
            Write-Note "The downloaded installer is not validly signed by the Python Software Foundation (status: $($signature.Status)). It was NOT run."
            return $false
        }
        Write-Ok "Installer signature verified (Python Software Foundation)"

        Write-Info "Installing Python for your account only (no administrator rights needed)..."
        $installArgs = @('/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0', 'Include_test=0', 'Shortcuts=0')
        $process = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            if (Test-InstallerUnavailable -ExitCode $process.ExitCode) {
                Write-Note "The Python installer cannot run in this kind of session (Windows Installer error $($process.ExitCode)). Skipping it."
            } else {
                Write-Note "The Python installer finished with exit code $($process.ExitCode)."
            }
            return $false
        }
        return $true
    } catch {
        Write-Note "Could not download or install Python: $($_.Exception.Message)"
        return $false
    } finally {
        if (Test-Path -LiteralPath $workDir) {
            Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-QuietNative {
    # Runs a program with its output captured in files, printing a dot every two seconds
    # so a long step is visibly alive (this also works when output is not a console).
    # Returns the exit code. The operating system does the redirection, so nothing the
    # program writes can surface as a PowerShell error.
    param([string]$Exe, [string[]]$Arguments, [string]$StdoutPath, [string]$StderrPath, [switch]$NoDots)
    $argumentLine = Join-NativeArguments -Arguments $Arguments
    $process = Start-Process -FilePath $Exe -ArgumentList $argumentLine -NoNewWindow -PassThru `
        -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
    $null = $process.Handle   # Windows PowerShell 5.1 loses ExitCode unless the handle is held
    $ticks = 0
    while (-not $process.HasExited) {
        Start-Sleep -Milliseconds 500
        $ticks++
        if (-not $NoDots) {
            if ($ticks % 4 -eq 0) { Write-Host '.' -NoNewline }
        }
    }
    $process.WaitForExit()
    return [int]$process.ExitCode
}

function Test-WizardImportable {
    # Can this environment load the setup wizard? Output is captured, so a broken
    # install shows up as "needs installing", not as a Python traceback on screen.
    param([string]$VenvPython)
    $token = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $probeOut = Join-Path $env:TEMP "entrabot-import-$token.out.txt"
    $probeErr = Join-Path $env:TEMP "entrabot-import-$token.err.txt"
    try {
        $code = Invoke-QuietNative -Exe $VenvPython -Arguments @('-c', 'import entrabot.winsetup') `
            -StdoutPath $probeOut -StderrPath $probeErr -NoDots
        return ($code -eq 0)
    } finally {
        foreach ($file in @($probeOut, $probeErr)) {
            if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
        }
    }
}

function Test-DirectoryHasContent {
    param([string]$Path)
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    $first = Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue | Select-Object -First 1
    return ($null -ne $first)
}

function Stop-ProjectMcpServer {
    # A running entrabot-mcp.exe from THIS project's .venv locks files pip must replace.
    param([string]$VenvDir)
    $prefix = $VenvDir.TrimEnd('\') + '\'
    foreach ($process in @(Get-Process -Name 'entrabot-mcp' -ErrorAction SilentlyContinue)) {
        $path = ''
        try { $path = [string]$process.Path } catch { $path = '' }
        if (-not $path) { continue }
        if (-not $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        Write-Note "The Entrabot server from this folder is running (process $($process.Id)). Stopping it so its files can be updated; your AI host starts it again when needed."
        $taskkillArgs = @('/PID', [string]$process.Id, '/T', '/F')
        $null = & taskkill.exe @taskkillArgs
        if ($LASTEXITCODE -ne 0) {
            Stop-Setup "Could not stop the running Entrabot server (process $($process.Id))." @(
                'Close Claude Code / Copilot CLI windows that use this folder, then run setup again.')
        }
    }
}

# =============================================================================
# Main
# =============================================================================

$bound = @{}
foreach ($name in $PSBoundParameters.Keys) { $bound[$name] = $PSBoundParameters[$name] }

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

# Managed PCs can run PowerShell in Constrained Language Mode, where the .NET calls a
# setup like this needs are blocked. Say so plainly instead of failing half-way.
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    Write-Host ""
    Write-Host "  [xx] This PC restricts PowerShell (language mode: $($ExecutionContext.SessionState.LanguageMode))." -ForegroundColor Red
    Write-Host "       That is an application-control policy set by your organization. It also"
    Write-Host "       blocks the programs Entrabot installs, so setup cannot continue here."
    Write-Host "       What to tell IT: 'I need to run Python and local PowerShell scripts from"
    Write-Host "       my user profile for a development tool (Entrabot).'"
    Write-Host ""
    exit 1
}

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Strict mode makes reading $LASTEXITCODE an error until some native command has set it.
$global:LASTEXITCODE = 0

$problems = @(Get-ParameterProblems -Bound $bound)
if ($problems.Count -gt 0) {
    foreach ($problem in $problems) { Write-Host "ERROR: $problem" -ForegroundColor Red }
    exit 2
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

if ($Status) {
    & (Join-Path $ProjectRoot 'status-windows.ps1') -Json:$Json -HealthOnly:$HealthOnly -Strict:$Strict
    exit $LASTEXITCODE
}

# ---- 1. This PC ---------------------------------------------------------------
if ($env:WSL_DISTRO_NAME) {
    Write-Host "ERROR: setup-windows.ps1 invoked from inside WSL." -ForegroundColor Red
    Write-Host "  WSL is a Linux environment; run scripts/setup.sh instead." -ForegroundColor Red
    Write-Host "  To set up native Windows, run scripts\setup-windows.cmd from a" -ForegroundColor Red
    Write-Host "  Windows terminal (not a WSL shell)." -ForegroundColor Red
    exit 1
}
if ($env:OS -ne 'Windows_NT') {
    Write-Host "ERROR: setup-windows.ps1 must run on Windows. On macOS or Linux run scripts/setup.sh." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Entrabot setup" -ForegroundColor Cyan
Write-Host "Getting this PC ready, then the setup wizard takes over."

if ($bound.ContainsKey('A365AgentName')) {
    Write-Note "-A365AgentName is deprecated and ignored (Work IQ setup uses the existing Blueprint)."
}
if ($Migrate) {
    Write-Note "-Migrate is deprecated and ignored (old data is moved automatically)."
}

Write-Step "[1/4] Checking this PC"

if ([System.Environment]::OSVersion.Version.Major -lt 10) {
    Stop-Setup "Entrabot needs Windows 10 or Windows 11." @()
}

$elevated = Test-Elevated
if ($elevated) {
    $currentSid = Get-CurrentSid
    $ownerSid = Get-SessionOwnerSid
    if (Test-DifferentAccount -CurrentSid $currentSid -SessionOwnerSid $ownerSid -Elevated $elevated) {
        $who = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        Stop-Setup "This window is running as a different account ($who) than the person logged on to this PC." @(
            'Entrabot stores its certificate, credentials and settings for the account that runs',
            'setup. Set up this way, the agent would belong to that administrator account and the',
            'person actually using this PC could not start it.',
            '',
            'Fix: close this window and run scripts\setup-windows.cmd normally (double-click it).',
            'Setup does not need administrator rights.')
    }
    Write-Note "This window is running as Administrator. Setup does not need that; continuing."
}

$diagnoseOnly = [bool]$Diagnose          # -Diagnose inspects; it never installs or changes anything
$verboseRun = Test-BoundSwitch $bound 'Verbose'

# A ZIP downloaded from the internet marks every file as blocked. Clear that for the
# project's own scripts so later steps (and later runs) are not refused.
if (-not $diagnoseOnly) {
    try {
        $marked = Get-Item -LiteralPath $PSCommandPath -Stream 'Zone.Identifier' -ErrorAction SilentlyContinue
        if ($marked) { Write-Info "This copy of Entrabot was downloaded from the internet; unblocking its scripts." }
        $toUnblock = @(Get-ChildItem -LiteralPath $ProjectRoot -File -ErrorAction SilentlyContinue)
        $toUnblock += @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'scripts') -File -Recurse -ErrorAction SilentlyContinue)
        foreach ($file in $toUnblock) {
            if (@('.ps1', '.cmd', '.py', '.sh', '.psm1') -contains $file.Extension.ToLowerInvariant()) {
                Unblock-File -LiteralPath $file.FullName
            }
        }
    } catch {
        Write-Note "Could not unblock the downloaded scripts: $($_.Exception.Message)"
    }
}

Write-Ok "Windows $([System.Environment]::OSVersion.Version), project folder: $ProjectRoot"

# ---- 2. Python ------------------------------------------------------------------
Write-Step "[2/4] Python"

$venvDir = Join-Path $ProjectRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$legacyData = Join-Path $env:USERPROFILE '.entrabot'
$currentData = Join-Path $env:LOCALAPPDATA 'entrabot'
$notSetUpFix = @(
    '-Diagnose only inspects an existing setup; it never installs or changes anything.',
    'Run scripts\setup-windows.cmd (without -Diagnose) to set Entrabot up here.')

$python = $null
$venvVersion = ''
if (Test-Path -LiteralPath $venvPython) { $venvVersion = Get-PythonVersionText -Exe $venvPython }
$venvOk = Test-PythonVersionOk -Text $venvVersion

if ($venvOk) {
    # A working environment proves its interpreter is still there; nothing to find.
    Write-Ok (Get-PythonRouteMessage -Route 'venv' -Version $venvVersion)
}
if ($diagnoseOnly) {
    if (-not $venvOk) {
        Stop-Setup "Entrabot is not set up in this folder yet (there is no working Python environment here)." $notSetUpFix
    }
}
if (-not $venvOk) {
    # Route 1: a real Python that is already on this PC.
    $route = 'existing'
    $python = Find-Python

    # Route 2: Entrabot's own private copy (official signed package; no installer).
    if (-not $python) {
        # The private copy lives under the data folder. Settle data from the old
        # location first, or writing here would make it look like two data folders.
        $earlyAction = Get-LegacyDataAction -LegacyHasContent (Test-DirectoryHasContent -Path $legacyData) `
            -CurrentHasContent (Test-DirectoryHasContent -Path $currentData)
        if ($earlyAction -eq 'move') {
            try {
                Move-LegacyData -Legacy $legacyData -Current $currentData
                Write-Ok "Moved your existing Entrabot data to $currentData"
            } catch {
                Stop-Setup "Could not move older Entrabot data to its current location: $($_.Exception.Message)" @(
                    "Old location: $legacyData", "Current location: $currentData",
                    'Move the old folder there yourself (or delete it if you do not need it), then run setup again.')
            }
        }
        $private = Get-PrivatePython -DataRoot $currentData
        if ($private) {
            if (Test-PythonVersionOk -Text (Get-PythonVersionText -Exe $private.Path)) {
                $python = $private.Path
                if ($private.Downloaded) { $route = 'private-new' } else { $route = 'private' }
            } else {
                Write-Note "The private copy of Python does not start on this PC; trying another way."
            }
        }
    }

    # Route 3: a per-user install with the Windows Package Manager.
    if (-not $python) {
        if (Test-WingetWorks) {
            Write-Info "Installing Python 3.12 for your account with the Windows Package Manager (winget)..."
            if (Install-PythonWithWinget) { $python = Find-Python; $route = 'winget' }
        }
    }

    # Route 4: the python.org installer, for this account only.
    if (-not $python) {
        if (Install-PythonFromPythonOrg) { $python = Find-Python; $route = 'pythonorg' }
    }

    # Route 5: plain instructions.
    if (-not $python) {
        Stop-Setup "Python could not be set up automatically." @(
            'Install it yourself, then run scripts\setup-windows.cmd again:',
            '  1. Open https://www.python.org/downloads/release/python-31210/',
            '  2. Download "Windows installer (64-bit)" (or "ARM64" on an ARM PC).',
            '  3. Run it and choose "Install Now". Administrator rights are not needed.',
            'Do not use the Python from the Microsoft Store; Entrabot cannot use that one.')
    }
    $pythonVersion = Get-PythonVersionText -Exe $python
    Write-Ok "$(Get-PythonRouteMessage -Route $route -Version $pythonVersion) ($python)"
}

# ---- 3. Entrabot in this folder ----------------------------------------------------
Write-Step "[3/4] Entrabot"

# "Installed" means: the environment can load the setup tools AND it was installed from
# the project definition as it is now. The stamp records the second half.
$installExtras = 'provisioning'   # the setup scripts import azure-identity and requests
$stampPath = Join-Path $venvDir 'entrabot-install.stamp'
$pyproject = Join-Path $ProjectRoot 'pyproject.toml'
$expectedStamp = ''
if (Test-Path -LiteralPath $pyproject -PathType Leaf) {
    $expectedStamp = Get-InstallStamp -PyprojectHash (Get-FileHash -LiteralPath $pyproject -Algorithm SHA256).Hash `
        -Extras $installExtras
}
$recordedStamp = ''
$importable = $false
if ($venvOk) {
    if (Test-Path -LiteralPath $stampPath -PathType Leaf) {
        $recordedStamp = [string](Get-Content -LiteralPath $stampPath -TotalCount 1)
    }
    $importable = Test-WizardImportable -VenvPython $venvPython
}
$installCurrent = ($importable -and (Test-InstallCurrent -Expected $expectedStamp -Recorded $recordedStamp))
$pipLog = ''

if ($diagnoseOnly) {
    if (-not $importable) {
        Stop-Setup "Entrabot is not fully installed in this folder (its Python environment cannot load the setup tools)." $notSetUpFix
    }
    Write-Ok "Entrabot is installed here"
} elseif ($installCurrent) {
    Write-Ok "Entrabot is already installed here"
} else {
    Stop-ProjectMcpServer -VenvDir $venvDir

    if (-not $venvOk) {
        if (Test-Path -LiteralPath $venvDir) {
            # Not ours to delete: it may be another machine's environment on a shared folder.
            $aside = '.venv.unusable-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
            Write-Note "The existing .venv cannot be used on this PC (missing, broken, or older than Python 3.12). Moving it aside to $aside; delete that folder whenever you like."
            Rename-Item -LiteralPath $venvDir -NewName $aside
        }
        Write-Info "Creating the Python environment (.venv)..."
        $venvArgs = @('-m', 'venv', $venvDir)
        & $python @venvArgs
        if ($LASTEXITCODE -ne 0) {
            Stop-Setup "Could not create the Python environment (python -m venv exited with $LASTEXITCODE)." @(
                "Interpreter used: $python")
        }
        if (-not (Test-PythonVersionOk -Text (Get-PythonVersionText -Exe $venvPython))) {
            Stop-Setup "The new Python environment does not start." @("Expected: $venvPython")
        }
        Write-Ok "Python environment ready"
    }

    if (Test-LongPathRisk -ProjectRoot $ProjectRoot -LongPathsEnabled (Get-LongPathsEnabled)) {
        Write-Note "This folder's path is long ($($ProjectRoot.Length) characters) and Windows long-path support is off, so installing packages may fail with 'path too long' errors. If it does, move the Entrabot folder somewhere shorter (for example C:\src\entrabot) and run setup again."
    }

    # pip's screen output (about 90 lines of "Collecting / Downloading") goes to capture
    # files; the screen gets one line and a dot every two seconds. -Verbose shows it all
    # instead. pip also writes its own complete log (--log). Everything starts in %TEMP%:
    # creating %LOCALAPPDATA%\entrabot before the data migration below would make an old
    # installation look like "two data folders".
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $pipLog = Join-Path $env:TEMP "entrabot-bootstrap-pip-$stamp.log"
    $pipOut = Join-Path $env:TEMP "entrabot-bootstrap-pip-$stamp.out.txt"
    $pipErr = Join-Path $env:TEMP "entrabot-bootstrap-pip-$stamp.err.txt"
    $pipArgs = @(
        '-m', 'pip', 'install', '--disable-pip-version-check', '--prefer-binary',
        '--log', $pipLog,
        '-e', ($ProjectRoot + '[' + $installExtras + ']')
    )
    if (Test-Path -LiteralPath $stampPath) { Remove-Item -LiteralPath $stampPath -Force }
    $pipShown = @()
    $pipStarted = Get-Date
    if ($verboseRun) {
        Write-Info "Installing Entrabot and its packages (full output, because of -Verbose):"
        & $venvPython @pipArgs
        $pipExit = $LASTEXITCODE
    } else {
        Write-Host "  Installing Entrabot and its packages (a minute or two) " -NoNewline
        $pipExit = Invoke-QuietNative -Exe $venvPython -Arguments $pipArgs -StdoutPath $pipOut -StderrPath $pipErr
        Write-Host ""
        foreach ($capture in @($pipOut, $pipErr)) {
            if (Test-Path -LiteralPath $capture) {
                $pipShown += @(Get-Content -LiteralPath $capture)
                Remove-Item -LiteralPath $capture -Force -ErrorAction SilentlyContinue
            }
        }
    }
    $pipSeconds = [int]((Get-Date) - $pipStarted).TotalSeconds

    if ($pipExit -ne 0) {
        if ($pipShown.Count -gt 0) {
            Write-Host ""
            Write-Host "  ---- last lines of the install output ----" -ForegroundColor DarkGray
            foreach ($line in @($pipShown | Select-Object -Last 25)) { Write-Host "  $line" -ForegroundColor DarkGray }
        }
        $fix = @("Full log: $pipLog")
        $allLines = @($pipShown)
        if (Test-Path -LiteralPath $pipLog) { $allLines += @(Get-Content -LiteralPath $pipLog) }
        if (Test-CompilerNeeded -Lines $allLines) {
            $fix += ''
            $fix += 'A package had to be compiled and this PC has no C++ compiler. Install one with:'
            $fix += '  powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithBuildTools'
            $fix += 'then run scripts\setup-windows.cmd again.'
        } else {
            $fix += 'If this looks like a network problem (proxy, VPN, offline), fix that and run setup again.'
        }
        Stop-Setup "Installing Entrabot's packages failed (pip exited with $pipExit)." $fix
    }
    if ($expectedStamp) { Set-Content -LiteralPath $stampPath -Value $expectedStamp -Encoding ascii }
    Write-Ok "Entrabot installed ($pipSeconds s)"
}

if (-not $diagnoseOnly) {
    # One-time move of data from the old location (%USERPROFILE%\.entrabot) to the current
    # one (%LOCALAPPDATA%\entrabot). No double quotes in the Python: Windows PowerShell 5.1
    # strips them from native arguments.
    $MigrationCode = 'from entrabot.config import migrate_legacy_data_dir as m; print(''migrated=%s'' % m())'
    $legacyAction = Get-LegacyDataAction -LegacyHasContent (Test-DirectoryHasContent -Path $legacyData) `
        -CurrentHasContent (Test-DirectoryHasContent -Path $currentData)
    if ($legacyAction -eq 'conflict') {
        Write-Note "Two Entrabot data folders exist. Entrabot uses $currentData. The older one, $legacyData, was left untouched; nothing was merged or deleted. If the older one holds data you still want, compare the two before deleting it."
    } elseif ($legacyAction -eq 'move') {
        $migrationArgs = @('-c', $MigrationCode)
        $migrationOut = & $venvPython @migrationArgs
        if ($LASTEXITCODE -ne 0) {
            Stop-Setup "Moving older Entrabot data to its current location failed (see the error above)." @(
                "Old location: $legacyData", "Current location: $currentData")
        }
        if ((@($migrationOut) -join ' ') -match 'migrated=True') {
            Write-Ok "Moved your existing Entrabot data to $currentData"
        }
    }

    # Now that the data folder is settled, keep the install log with the other logs.
    if ($pipLog -and (Test-Path -LiteralPath $pipLog)) {
        try {
            $logDir = Join-Path $currentData 'logs'
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
            $keptLog = Join-Path $logDir (Split-Path -Leaf $pipLog)
            Move-Item -LiteralPath $pipLog -Destination $keptLog -Force
            $pipLog = $keptLog
        } catch {
            Write-Info "(The install log stays at $pipLog)"
        }
    }
}

if ($BootstrapOnly) {
    Write-Host ""
    Write-Ok "Bootstrap complete. Python environment: $venvPython"
    if ($pipLog -and (Test-Path -LiteralPath $pipLog)) { Write-Info "Install log: $pipLog" }
    exit 0
}

# ---- 4. Hand over to the wizard ---------------------------------------------------
Write-Step "[4/4] Starting the setup wizard"

$wizardArgs = @(Get-WizardArguments -Bound $bound -ProjectRoot $ProjectRoot)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$handoff = @('-m', 'entrabot.winsetup') + $wizardArgs
& $venvPython @handoff
exit $LASTEXITCODE
