@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN.ps1" %*
set "runExit=%errorlevel%"
if not "%runExit%"=="0" echo The server command failed. See the error above.
if "%~1"=="" pause
exit /b %runExit%
