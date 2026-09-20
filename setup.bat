@echo off
rem Double-click this to set up the bot. It launches setup.ps1 with
rem PowerShell's script-blocking policy bypassed for this one run only
rem (nothing about your system settings is changed).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
