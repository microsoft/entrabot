@echo off
rem One-liner CMD wrapper that elevates PowerShell with the right
rem execution policy. Use this instead of running .ps1 directly so
rem users don't have to remember the -ExecutionPolicy flag.
pwsh -ExecutionPolicy Bypass -NoProfile -File "%~dp0setup-windows.ps1" %*
