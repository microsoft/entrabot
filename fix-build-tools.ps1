# Must run elevated (admin) for VS installer to work
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Re-launching as admin..."
    Start-Process pwsh -Verb RunAs -ArgumentList "-File `"$PSCommandPath`"" -Wait
    exit
}

$installer = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vs_installer.exe"

Write-Host "Installing Windows 11 SDK (this will take a few minutes)..."
$argString = 'modify --installPath "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools" --add Microsoft.VisualStudio.Component.Windows11SDK.26100 --quiet'
$proc = Start-Process -FilePath $installer -ArgumentList $argString -PassThru -Wait
Write-Host "Exit code: $($proc.ExitCode)"
Write-Host "Done. Press Enter to close."
Read-Host
