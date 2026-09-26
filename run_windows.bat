@echo off
REM ===================================================================
REM  Runs LinkedIn Enricher straight from the source code.
REM  Use this if you do not want to build the .exe.
REM  The first run takes a few minutes while it sets itself up.
REM ===================================================================
setlocal
cd /d "%~dp0"

call find_python.bat
if not defined PYEXE goto nopython

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  First run: setting things up. This takes a few minutes...
    echo  Using Python: %PYEXE%
    echo.
    %PYEXE% -m venv .venv
    if not exist ".venv\Scripts\python.exe" goto novenv
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet PySide6 openpyxl playwright keyring
    if errorlevel 1 goto nopip
    echo  Downloading the browser ^(about 150 MB, once only^)...
    ".venv\Scripts\python.exe" -m playwright install chromium
    if errorlevel 1 goto nopip
    echo  Ready.
)

".venv\Scripts\python.exe" li_app.py %*
if errorlevel 1 (
    echo.
    echo  The app closed with a problem. The message above is the important part.
    echo  Help ^> Check this computer, inside the app, shows more detail.
    echo.
    pause
)
exit /b 0

REM -------------------------------------------------------------------
:nopython
echo.
if "%PYWHY%"=="old" (
    echo  Python is installed, but it is too old. This needs 3.10 or newer.
    echo  Get a current version from https://www.python.org/downloads/
    echo  and TICK "Add python.exe to PATH" on the first installer screen.
) else (
    echo  I could not find a working Python on this PC.
    echo.
    echo  Not installed yet?
    echo      https://www.python.org/downloads/  --  and on the FIRST installer
    echo      screen, TICK "Add python.exe to PATH". Small checkbox, near the
    echo      bottom. Nothing works without it.
    echo.
    echo  Already installed, but "python" opens the Microsoft Store or says
    echo  "Python was not found"?
    echo      Windows is intercepting the command with a placeholder. Open
    echo        Settings ^> Apps ^> Advanced app settings ^> App execution aliases
    echo      and turn OFF "python.exe" and "python3.exe". Then try again.
)
echo.
pause
exit /b 1

:novenv
echo.
echo  Python ran, but could not create its private environment in .venv
echo.
echo    - is this folder inside Program Files, or read-only? Move it to your
echo      Desktop or Documents.
echo    - is OneDrive syncing it? Pause syncing, or use a local folder.
echo    - antivirus may have blocked it. Add this folder to its exclusions.
echo.
echo  Folder: %CD%
echo.
pause
exit /b 1

:nopip
echo.
echo  The download failed -- almost always the network, not your PC.
echo  A company firewall or proxy blocking pip is the usual cause.
echo.
echo  Nothing is broken. Run this again once the connection is sorted.
echo.
pause
exit /b 1
