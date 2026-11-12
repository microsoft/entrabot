@echo off
rem Entrabot setup for Windows. Double-click this file, or run it from a console:
rem     scripts\setup-windows.cmd
rem     scripts\setup-windows.cmd -NewChain -UpnSuffix bob -Yes
rem
rem It starts setup-windows.ps1 with the Windows PowerShell that ships with Windows
rem (nothing has to be installed first) and with the execution policy lifted for this
rem one run. One statement per line, no labels and no parenthesized blocks, so the file
rem behaves the same whether it arrives with Windows or Unix line endings.
setlocal
set "EB_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "EB_SETUP_PS1=%~dp0setup-windows.ps1"

rem Started from a PowerShell 7 terminal, this window inherits PowerShell 7's module
rem path, and Windows PowerShell would then fail to load its own in-box modules
rem (about_PSModulePath, "Starting Windows PowerShell from PowerShell 7"). With no
rem PSModulePath, Windows PowerShell builds its default one at startup.
set "PSModulePath="

rem Keep the window open at the end only when it would otherwise vanish: a window
rem that exists just to run this file was started as  cmd.exe /c "<this file> ...".
rem Never pause an unattended run (-Yes), a remote session, or a Windows Terminal tab,
rem which stays open by itself. This needs no process or WMI queries, so it cannot
rem fail or print an error. Delayed expansion is used only here, because it is the one
rem way to read CMDCMDLINE safely (the value is never parsed as a command), and is
rem switched off again before the arguments are passed on (it would eat "!" in them).
rem -Yes is looked for in CMDCMDLINE, never by copying %* into a variable: %* inside
rem quotes turns the caller's quoting inside out, so  -Subscription "R&D Sandbox"
rem would run part of the argument as a command. CMDCMDLINE only counts when it names
rem this file, and then it holds this file's arguments too.
set "EB_PAUSE=0"
setlocal EnableDelayedExpansion
set "EB_LINE=!CMDCMDLINE!"
if not "!EB_LINE: /c =!"=="!EB_LINE!" set "EB_PAUSE=1"
if "!EB_LINE:%~nx0=!"=="!EB_LINE!" set "EB_PAUSE=0"
if not "!EB_LINE: -Yes=!"=="!EB_LINE!" set "EB_PAUSE=0"
if defined SSH_CONNECTION set "EB_PAUSE=0"
if defined SSH_CLIENT set "EB_PAUSE=0"
if defined WT_SESSION set "EB_PAUSE=0"
endlocal & set "EB_PAUSE=%EB_PAUSE%"

rem Preflight (inline PowerShell, which runs even when scripts are disabled): exit code
rem 42 means scripts are blocked by Group Policy, which -ExecutionPolicy cannot override.
rem The script path travels in an environment variable, never inside PowerShell source,
rem so an apostrophe in the folder name cannot break anything.
"%EB_PS%" -NoProfile -Command "$ProgressPreference = 'SilentlyContinue'; $policy = @((Get-ExecutionPolicy -Scope MachinePolicy), (Get-ExecutionPolicy -Scope UserPolicy)); if ($policy -contains 'Restricted' -or $policy -contains 'AllSigned') { exit 42 }; Unblock-File -LiteralPath $env:EB_SETUP_PS1 -ErrorAction SilentlyContinue; exit 0"
set "EB_BLOCKED=0"
if "%ERRORLEVEL%"=="42" set "EB_BLOCKED=1"

if "%EB_BLOCKED%"=="1" echo.
if "%EB_BLOCKED%"=="1" echo   Entrabot setup cannot start on this PC.
if "%EB_BLOCKED%"=="1" echo.
if "%EB_BLOCKED%"=="1" echo   Your organization has turned off PowerShell scripts with a Group Policy
if "%EB_BLOCKED%"=="1" echo   (the MachinePolicy or UserPolicy execution policy). That setting cannot be
if "%EB_BLOCKED%"=="1" echo   overridden from here, and Entrabot setup is a PowerShell script.
if "%EB_BLOCKED%"=="1" echo.
if "%EB_BLOCKED%"=="1" echo   What to tell IT:
if "%EB_BLOCKED%"=="1" echo     I need to run a local PowerShell script, scripts\setup-windows.ps1, from the
if "%EB_BLOCKED%"=="1" echo     Entrabot folder in my user profile. Please allow locally created scripts for
if "%EB_BLOCKED%"=="1" echo     my account (execution policy RemoteSigned).
if "%EB_BLOCKED%"=="1" echo.
if "%EB_BLOCKED%"=="1" if "%EB_PAUSE%"=="1" pause
if "%EB_BLOCKED%"=="1" exit /b 1

"%EB_PS%" -NoProfile -ExecutionPolicy Bypass -File "%EB_SETUP_PS1%" %*
set "EB_RC=%ERRORLEVEL%"

if "%EB_PAUSE%"=="1" echo.
if "%EB_PAUSE%"=="1" pause
exit /b %EB_RC%
