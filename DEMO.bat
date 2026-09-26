@echo off
REM ===================================================================
REM  Opens the app with saved example data and no LinkedIn at all.
REM  Nothing connects to the internet. You cannot break anything.
REM  Run SETUP.bat first.
REM ===================================================================
setlocal
cd /d "%~dp0"

if exist "dist\LinkedInEnricher\LinkedInEnricher.exe" (
    "dist\LinkedInEnricher\LinkedInEnricher.exe" --demo
    exit /b 0
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" li_app.py --demo
    exit /b 0
)

echo.
echo  Nothing is set up yet. Please run SETUP.bat first.
echo.
pause
exit /b 1
