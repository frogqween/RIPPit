@echo off
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Create RIPPit Shortcut.ps1"
if %ERRORLEVEL% EQU 0 (
  echo Shortcut created on your Desktop. Right-click it and choose "Pin to taskbar".
  pause
) else (
  echo Failed to create the shortcut.
  pause
)
