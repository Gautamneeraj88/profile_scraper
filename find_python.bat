@echo off
REM ===================================================================
REM  Works out how to run Python on this PC and returns it in PYEXE.
REM
REM  Called by build.bat and run_windows.bat -- not meant to be run
REM  on its own.
REM
REM  Why this exists: Windows ships a fake python.exe that only prints
REM  "Python was not found; run without arguments to install from the
REM  Microsoft Store".  It is a real file, so checking whether the file
REM  exists proves nothing.  The only reliable test is to run Python
REM  and see whether it answers.
REM
REM  Sets:  PYEXE   how to run it, e.g. "py -3" or a full path
REM         PYWHY   why it failed, when it did:  none | old
REM ===================================================================

set "PYEXE="
set "PYWHY=none"

REM -- 1. The py launcher.  Installed with Python itself and immune to
REM       the Store stub, so it is the best answer when it is there.
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 9)" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py -3"
    goto :found
)
if errorlevel 9 if not errorlevel 10 set "PYWHY=old"

REM -- 2. Plain "python" on the PATH.  The Store stub fails this test
REM       because it never runs any Python at all.
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 9)" >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python"
    goto :found
)
if errorlevel 9 if not errorlevel 10 set "PYWHY=old"

REM -- 3. The usual install folders, for when Python is installed but
REM       "Add python.exe to PATH" was never ticked.
for %%V in (314 313 312 311 310) do (
    call :try "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    if defined PYEXE goto :found
    call :try "%PROGRAMFILES%\Python%%V\python.exe"
    if defined PYEXE goto :found
    call :try "C:\Python%%V\python.exe"
    if defined PYEXE goto :found
)

exit /b 1

:try
if not exist %1 exit /b 0
%1 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 9)" >nul 2>nul
if not errorlevel 1 set "PYEXE=%~1"
if errorlevel 9 if not errorlevel 10 set "PYWHY=old"
exit /b 0

:found
exit /b 0
