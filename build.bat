@echo off
REM ===================================================================
REM  Builds LinkedIn Enricher into a Windows application.
REM  Run this once on a Windows PC that has Python 3.10 or newer.
REM  You end up with:  dist\LinkedInEnricher\LinkedInEnricher.exe
REM ===================================================================
setlocal
cd /d "%~dp0"

echo.
echo  Building LinkedIn Enricher
echo  =========================
echo.

call find_python.bat
if not defined PYEXE goto nopython
echo  Using Python: %PYEXE%
echo.

echo  [1/5] Creating a private Python environment...
if not exist ".venv\Scripts\python.exe" (
    %PYEXE% -m venv .venv
    if errorlevel 1 goto novenv
)
if not exist ".venv\Scripts\python.exe" goto novenv
set "VPY=.venv\Scripts\python.exe"

echo  [2/5] Installing the libraries the app needs...
"%VPY%" -m pip install --quiet --upgrade pip
"%VPY%" -m pip install --quiet PySide6 openpyxl playwright keyring pyinstaller
if errorlevel 1 goto nopip

echo  [3/5] Downloading the browser the app drives (about 150 MB, once only)...
"%VPY%" -m playwright install chromium
if errorlevel 1 goto nopip

echo  [4/5] Checking the engine still behaves...
"%VPY%" tests_engine.py
if errorlevel 1 (
    echo.
    echo  The self-checks did not pass. Stopping so you do not ship a broken build.
    echo  Please send the output above to whoever maintains this tool.
    echo.
    pause
    exit /b 1
)

echo  [5/5] Building the application...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
"%VPY%" -m PyInstaller --noconfirm --clean LinkedInEnricher.spec
if errorlevel 1 goto nobuild

echo.
echo  ===================================================================
echo   Done.
echo.
echo   Your application is here:
echo       dist\LinkedInEnricher\LinkedInEnricher.exe
echo.
echo   Try it first with no LinkedIn at all:
echo       dist\LinkedInEnricher\LinkedInEnricher.exe --demo
echo.
echo   To give it to someone else, send them the whole
echo   dist\LinkedInEnricher folder (zip it first).
echo  ===================================================================
echo.
pause
exit /b 0

REM -------------------------------------------------------------------
:nopython
echo.
if "%PYWHY%"=="old" (
    echo  Python is installed, but it is too old. This needs 3.10 or newer.
    echo.
    echo  Install a current version from:
    echo      https://www.python.org/downloads/
    echo  and TICK "Add python.exe to PATH" on the first installer screen.
) else (
    echo  I could not find a working Python on this PC.
    echo.
    echo  If you have NOT installed Python yet:
    echo      Get it from https://www.python.org/downloads/
    echo      On the FIRST installer screen, TICK "Add python.exe to PATH".
    echo      That checkbox is small and near the bottom. Nothing works without it.
    echo.
    echo  If you HAVE installed Python and typing "python" opens the Microsoft
    echo  Store, or prints "Python was not found":
    echo      Windows is intercepting the command with a placeholder.
    echo      Open  Settings ^> Apps ^> Advanced app settings ^>
    echo            App execution aliases
    echo      and turn OFF both "python.exe" and "python3.exe".
    echo      Then close this window and run build.bat again.
    echo.
    echo  Either way, this is fixed in a couple of minutes.
)
echo.
pause
exit /b 1

:novenv
echo.
echo  Python ran, but could not create its private environment in .venv
echo.
echo  Usually one of:
echo    - this folder is read-only, or inside Program Files. Move it to your
echo      Desktop or Documents and try again.
echo    - OneDrive is syncing this folder and has it locked. Pause syncing,
echo      or move the folder somewhere local.
echo    - antivirus blocked it. Add this folder to its exclusions.
echo.
echo  Folder: %CD%
echo.
pause
exit /b 1

:nopip
echo.
echo  The download failed. Almost always the network, not your PC.
echo.
echo    - no internet connection
echo    - a company firewall or proxy blocking pip. Try a home connection,
echo      or ask IT for the proxy settings.
echo    - antivirus interfering.
echo.
echo  Nothing is broken. Run build.bat again once the connection is sorted;
echo  it carries on from where it stopped.
echo.
pause
exit /b 1

:nobuild
echo.
echo  Everything installed, but building the .exe failed.
echo.
echo  Antivirus blocking PyInstaller is the usual cause, because it writes
echo  a brand-new executable.
echo.
echo  You do not need the .exe to use the tool. Double-click
echo  run_windows.bat instead -- same application, run from the source.
echo.
pause
exit /b 1
