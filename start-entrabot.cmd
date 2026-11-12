@echo off
rem Start your Entrabot agent: double-click this file.
rem
rem It starts Claude Code in this folder with the Entrabot channel switched on, so the
rem agent hears Teams messages as they arrive. Anything you add after the file name is
rem passed on to Claude Code:   start-entrabot.cmd --resume
rem
rem Run scripts\setup-windows.cmd once before the first start.
rem
rem One statement per line, with no labels and no parenthesized blocks, so the file
rem behaves the same whether it arrives with Windows or Unix line endings and whatever
rem characters the folder name contains.
setlocal
cd /d "%~dp0"

rem Keep the window open after a problem only when it would otherwise vanish: a window
rem that exists just to run this file was started as  cmd.exe /c "<this file>". A
rem remote session or a Windows Terminal tab stays open by itself. Delayed expansion is
rem the one safe way to read CMDCMDLINE; it is switched off again before the arguments
rem are used, because it would eat exclamation marks in them.
set "EB_PAUSE=0"
setlocal EnableDelayedExpansion
set "EB_LINE=!CMDCMDLINE!"
if not "!EB_LINE: /c =!"=="!EB_LINE!" set "EB_PAUSE=1"
if defined SSH_CONNECTION set "EB_PAUSE=0"
if defined SSH_CLIENT set "EB_PAUSE=0"
if defined WT_SESSION set "EB_PAUSE=0"
endlocal & set "EB_PAUSE=%EB_PAUSE%"

rem 1. Has setup been run in this folder?
set "EB_READY=1"
if not exist ".venv\Scripts\entrabot-mcp.exe" set "EB_READY=0"
if not exist ".mcp.json" set "EB_READY=0"
if "%EB_READY%"=="0" echo Entrabot is not set up in this folder yet. Run scripts\setup-windows.cmd first.
if "%EB_READY%"=="0" if "%EB_PAUSE%"=="1" pause
if "%EB_READY%"=="0" exit /b 1

rem 2. Find Claude Code: on PATH, else in the folder its own installer uses.
set "EB_CLAUDE="
for %%I in (claude.exe claude.cmd) do if not defined EB_CLAUDE set "EB_CLAUDE=%%~$PATH:I"
if not defined EB_CLAUDE if exist "%USERPROFILE%\.local\bin\claude.exe" set "EB_CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
if not defined EB_CLAUDE echo Claude Code is not installed. To install it, run this from the Entrabot folder:
if not defined EB_CLAUDE echo     powershell -ExecutionPolicy Bypass -File scripts\prereqs-windows.ps1 -WithClaudeCode
if not defined EB_CLAUDE if "%EB_PAUSE%"=="1" pause
if not defined EB_CLAUDE exit /b 1

rem 3. Claude Code runs this project's safety hooks through Git Bash. It finds Git Bash
rem under Program Files by itself; a copy installed just for your account needs pointing
rem out. Set for this window only, and only when you have not set it yourself.
if not defined CLAUDE_CODE_GIT_BASH_PATH if not exist "%ProgramFiles%\Git\bin\bash.exe" if exist "%LOCALAPPDATA%\Programs\Git\bin\bash.exe" set "CLAUDE_CODE_GIT_BASH_PATH=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"

rem 4. Start Claude Code. A .cmd shim (an npm install) has to be started with CALL; the
rem native claude.exe must not be, because CALL re-reads the line and would change
rem percent signs and carets in your arguments.
set "EB_CALL="
if /i "%EB_CLAUDE:~-4%"==".cmd" set "EB_CALL=call"
%EB_CALL% "%EB_CLAUDE%" --dangerously-load-development-channels server:entrabot %*
set "EB_RC=%ERRORLEVEL%"

if not "%EB_RC%"=="0" if "%EB_PAUSE%"=="1" echo.
if not "%EB_RC%"=="0" if "%EB_PAUSE%"=="1" echo Claude Code ended with exit code %EB_RC%.
if not "%EB_RC%"=="0" if "%EB_PAUSE%"=="1" pause
exit /b %EB_RC%
