@echo off
setlocal
cd /d "%~dp0"
set "DEMO_PYTHON=%TEMP%\dbms-phase3-python314\tools\python.exe"
if exist "%DEMO_PYTHON%" goto run
set "DEMO_PYTHON=python"
:run
start "" "%~dp0index.html"
"%DEMO_PYTHON%" -B server.py %*
if errorlevel 1 pause
