@echo off
REM ===================================================================
REM  Reports what is and is not set up on this PC, and writes the
REM  report to a file you can send for help.
REM  Safe to run at any time. Changes nothing.
REM ===================================================================
setlocal
cd /d "%~dp0"

echo.
echo  LinkedIn Enricher -- checking this PC
echo  ====================================
echo.
echo  Folder: %CD%
echo.

call find_python.bat
if defined PYEXE (
    echo  Python .................. %PYEXE%
) else (
    if "%PYWHY%"=="old" (
        echo  Python .................. TOO OLD -- needs 3.10 or newer
    ) else (
        echo  Python .................. NOT FOUND -- run SETUP.bat
    )
)

if exist ".venv\Scripts\python.exe" (
    echo  Private environment ..... yes
) else (
    echo  Private environment ..... no -- run SETUP.bat
)

if exist "dist\LinkedInEnricher\LinkedInEnricher.exe" (
    echo  Built application ....... yes
) else (
    echo  Built application ....... no ^(not required; the app runs from source^)
)

if exist "out\*.json" (
    echo  Example profiles ........ yes
) else (
    echo  Example profiles ........ none ^(expected for the GitHub download^)
)

echo.
if not exist ".venv\Scripts\python.exe" goto done

echo  Asking the app about itself...
echo.
if exist "dist\LinkedInEnricher\LinkedInEnricher.exe" (
    "dist\LinkedInEnricher\LinkedInEnricher.exe" --doctor
) else (
    ".venv\Scripts\python.exe" li_app.py --doctor
)

:done
echo.
echo  ===================================================================
echo   A copy of the app's report was saved to:
echo       %%APPDATA%%\GradNext\LinkedInEnricher\diagnostics.txt
echo.
echo   Send that file if you need help.
echo  ===================================================================
echo.
pause
exit /b 0
