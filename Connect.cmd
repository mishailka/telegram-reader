@echo off
set "TR_CONNECT=%USERPROFILE%\plugins\telegram-reader\Connect-local.ps1"
if not exist "%TR_CONNECT%" (
  echo Run Install.cmd first.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%TR_CONNECT%"
if errorlevel 1 pause
