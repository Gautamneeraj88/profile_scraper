@echo off
REM ===================================================================
REM  LinkedIn Enricher -- one-click setup for Windows.
REM
REM  Double-click this file.  It does everything:
REM
REM    - finds Python, and offers to install it if there is none
REM    - creates a private environment, so nothing else is touched
REM    - installs the libraries and the browser
REM    - checks the engine works
REM    - builds LinkedInEnricher.exe
REM    - puts a shortcut on your Desktop
REM
REM  Everything it does is written to setup-log.txt.  If something
REM  goes wrong, that file is what to send for help.
REM
REM  SETUP.bat /auto   installs Python without asking.
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "LOG=%CD%\setup-log.txt"
set "AUTO="
if /i "%~1"=="/auto" set "AUTO=1"

echo LinkedIn Enricher setup > "%LOG%"
echo Started %DATE% %TIME% >> "%LOG%"
echo Folder: %CD% >> "%LOG%"
echo. >> "%LOG%"

cls
echo.
echo   ===============================================
echo    LinkedIn Enricher  --  setup
echo   ===============================================
echo.
echo    This takes 10 to 20 minutes the first time,
echo    mostly downloading. You can leave it running.
echo.
echo    Progress is saved to setup-log.txt
echo.

REM ---------------------------------------------------------------- 1
echo   [1 of 6] Looking for Python...
call find_python.bat
if defined PYEXE goto havepython

if "%PYWHY%"=="old" (
    echo           Found Python, but it is too old ^(need 3.10 or newer^).
) else (
    echo           No Python on this PC.
)
echo No usable Python found. PYWHY=%PYWHY% >> "%LOG%"
echo.

where winget >nul 2>nul
if errorlevel 1 goto manualpython

if not defined AUTO (
    echo   Python can be installed automatically now. It installs for your
    echo   user account only and changes nothing else on this PC.
    echo.
    choice /c YN /m "  Install Python now"
    if errorlevel 2 goto manualpython
)

echo.
echo           Installing Python. This takes a few minutes...
echo --- winget install Python --- >> "%LOG%"
winget install -e --id Python.Python.3.12 --scope user --silent ^
    --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1

echo           Looking again...
call find_python.bat
if defined PYEXE goto havepython

echo.
echo           Python installed, but this window still cannot see it.
echo           Close this window, open the folder again, and run SETUP.bat
echo           once more. It will find it the second time.
echo.
echo Python installed but not visible in this session >> "%LOG%"
pause
exit /b 1

:havepython
echo           Using: %PYEXE%
echo Python: %PYEXE% >> "%LOG%"
%PYEXE% --version >> "%LOG%" 2>&1

REM ---------------------------------------------------------------- 2
echo   [2 of 6] Creating a private environment...
echo --- venv --- >> "%LOG%"
if not exist ".venv\Scripts\python.exe" (
    %PYEXE% -m venv .venv >> "%LOG%" 2>&1
)
if not exist ".venv\Scripts\python.exe" goto novenv
set "VPY=%CD%\.venv\Scripts\python.exe"
echo           Done.

REM ---------------------------------------------------------------- 3
echo   [3 of 6] Installing libraries ^(a few minutes^)...
echo --- pip --- >> "%LOG%"
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install PySide6 openpyxl playwright keyring pyinstaller >> "%LOG%" 2>&1
if errorlevel 1 goto nopip
echo           Done.

REM ---------------------------------------------------------------- 4
echo   [4 of 6] Downloading the browser ^(about 150 MB, once^)...
echo --- playwright --- >> "%LOG%"
"%VPY%" -m playwright install chromium >> "%LOG%" 2>&1
if errorlevel 1 goto nopip
echo           Done.

REM ---------------------------------------------------------------- 5
echo   [5 of 6] Checking the engine works...
echo --- tests --- >> "%LOG%"
"%VPY%" tests_engine.py >> "%LOG%" 2>&1
if errorlevel 1 goto testsfailed
echo           Passed.

REM ---------------------------------------------------------------- 6
echo   [6 of 6] Building the application...
echo --- pyinstaller --- >> "%LOG%"
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
"%VPY%" -m PyInstaller --noconfirm --clean LinkedInEnricher.spec >> "%LOG%" 2>&1

set "TARGET="
if exist "dist\LinkedInEnricher\LinkedInEnricher.exe" (
    set "TARGET=%CD%\dist\LinkedInEnricher\LinkedInEnricher.exe"
    set "WORKDIR=%CD%\dist\LinkedInEnricher"
    echo           Built: dist\LinkedInEnricher\LinkedInEnricher.exe
    echo Built exe OK >> "%LOG%"
) else (
    echo           The .exe did not build -- see setup-log.txt.
    echo           Not a problem: the app runs from the source instead.
    echo Exe build failed; falling back to source >> "%LOG%"
    set "TARGET=%CD%\run_windows.bat"
    set "WORKDIR=%CD%"
)

REM --------------------------------------------------- Desktop shortcut
echo.
echo   Putting a shortcut on your Desktop...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\make_shortcut.ps1" ^
    -Target "!TARGET!" -WorkDir "!WORKDIR!" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo           Could not create the shortcut. Start it from this folder instead.
) else (
    echo           Done -- look for "LinkedIn Enricher" on your Desktop.
)

REM ---------------------------------------------------------------- end
echo.
echo   ===============================================
echo    Setup finished.
echo   ===============================================
echo.
echo    Start the app from the Desktop shortcut, or from here:
echo        !TARGET!
echo.
echo    Before using your real LinkedIn account, try DEMO.bat --
echo    the whole app, with example data, connecting to nothing.
echo.
echo    CHECK.bat reports what is set up, if anything looks wrong.
echo.
echo    Full details: setup-log.txt
echo.
echo Finished OK %DATE% %TIME% >> "%LOG%"
pause
exit /b 0

REM ===================================================================
:manualpython
echo.
echo   -----------------------------------------------------------
echo    Please install Python by hand, then run SETUP.bat again.
echo.
echo      1. Go to  https://www.python.org/downloads/
echo      2. Download and run the installer.
echo      3. On the FIRST screen, TICK "Add python.exe to PATH".
echo         Small checkbox near the bottom. It matters.
echo      4. Close this window and run SETUP.bat again.
echo.
echo    Already installed Python, but it says "Python was not found"?
echo    Windows is intercepting the command. Open:
echo        Settings ^> Apps ^> Advanced app settings ^> App execution aliases
echo    and turn OFF "python.exe" and "python3.exe".
echo   -----------------------------------------------------------
echo.
pause
exit /b 1

:novenv
echo.
echo   Python ran, but could not set up its environment here.
echo.
echo     - Is this folder inside Program Files, or read-only?
echo       Move it to your Desktop or Documents and try again.
echo     - Is OneDrive syncing this folder? Pause syncing.
echo     - Antivirus may have blocked it. Add this folder to its exclusions.
echo.
echo   Folder: %CD%
echo   Details: setup-log.txt
echo.
pause
exit /b 1

:nopip
echo.
echo   A download failed. This is almost always the network, not your PC:
echo.
echo     - no internet connection
echo     - a company firewall or proxy blocking pip
echo     - antivirus interfering
echo.
echo   Nothing is broken. Run SETUP.bat again when the connection is
echo   sorted -- it carries on from where it stopped.
echo.
echo   Details: setup-log.txt
echo.
pause
exit /b 1

:testsfailed
echo.
echo   The engine's own checks did not pass, so the build was stopped
echo   rather than produce a copy that misbehaves.
echo.
echo   Please send setup-log.txt to whoever gave you this tool.
echo.
pause
exit /b 1
