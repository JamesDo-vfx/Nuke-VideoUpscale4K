@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
set "install_status=%errorlevel%"
pause
exit /b %install_status%
