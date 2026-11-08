@echo off
setlocal
"%~dp0runtime\python.exe" -B -X utf8 "%~dp0install.py"
set "install_status=%errorlevel%"
pause
exit /b %install_status%
