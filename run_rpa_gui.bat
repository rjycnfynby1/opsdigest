@echo off
chcp 65001 >nul
title RPA: %~1
cd /d "%~dp0"

rem ============================================================================
rem  run_rpa_gui.bat - launch RPA script from OpsDigest GUI.
rem  Opens in a separate console window (CREATE_NEW_CONSOLE)
rem  so Playwright browser is not blocked by parent process.
rem
rem  Parameters (positional, passed by server.py):
rem    %1 - script name (opsdigest_export)
rem    %2 - support group (may contain spaces, in quotes)
rem    %3 - statuses comma-separated (or empty)
rem    %4 - date after (DD.MM.YYYY, or empty)
rem    %5 - date before (DD.MM.YYYY, or empty)
rem    %6 - no_wait: 1 or empty
rem    %7 - output_dir (path, may contain spaces, in quotes)
rem    %8 - no_status: 1 or empty (search without status filter)
rem    %9 - time after (HH:MM, or empty)
rem    %10 - time before (HH:MM, or empty)
rem ============================================================================

set "VENV=%LOCALAPPDATA%\OpsDigest\venv"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"

rem --- Check venv ---
if not exist "%VENV%\Scripts\python.exe" (
    echo.
    echo   [!] Virtual environment not found: %VENV%
    echo       Run OpsDigest.bat at least once.
    echo.
    pause
    exit /b 1
)

set "PY=%VENV%\Scripts\python.exe"
set "SCRIPT=%~1"

rem --- Check script ---
if not exist "%~dp0%SCRIPT%.py" (
    echo.
    echo   [!] Script not found: %SCRIPT%.py
    echo.
    pause
    exit /b 1
)

rem --- Build arguments ---
set "ARGS=--url https://itsm.example.com/sm/index.do --channel chrome --profile rpa_profile"
if not "%~2"=="" set "ARGS=%ARGS% --group "%~2""
if "%~8"=="1" set "ARGS=%ARGS% --no-status"
if not "%~3"=="" set "ARGS=%ARGS% --statuses "%~3""
if not "%~4"=="" set "ARGS=%ARGS% --date-after %~4"
if not "%~5"=="" set "ARGS=%ARGS% --date-before %~5"
if not "%~9"=="" set "ARGS=%ARGS% --time-after %~9"
if not "%~10"=="" set "ARGS=%ARGS% --time-before %~10"
if "%~6"=="1" set "ARGS=%ARGS% --no-wait"
if not "%~7"=="" set "ARGS=%ARGS% --output-dir "%~7""

echo.
echo   === RPA: %SCRIPT% ===
echo   Args: %ARGS%
echo.
echo   Browser will open in a few seconds...
echo   Do not close this window until finished.
echo.

"%PY%" "%~dp0%SCRIPT%.py" %ARGS%

echo.
echo   === Done (code: %errorlevel%) ===
echo   Output: %~dp0opsdigest_output\
pause
