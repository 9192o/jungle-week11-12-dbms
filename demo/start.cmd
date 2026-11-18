@echo off
setlocal
cd /d "%~dp0" || exit /b 1
set "DEMO_PYTHON=%TEMP%\dbms-phase3-python314\tools\python.exe"
if exist "%DEMO_PYTHON%" goto run
set "DEMO_PYTHON=python"
:run
"%DEMO_PYTHON%" -B server.py --open-browser %*
set "DEMO_EXIT=%ERRORLEVEL%"
if not "%DEMO_EXIT%"=="0" pause
exit /b %DEMO_EXIT%
