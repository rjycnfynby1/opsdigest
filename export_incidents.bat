@echo off
chcp 65001 >nul
title Export Incidents (ITSM)
cd /d "%~dp0"
rem ============================================================================
rem  Export incidents from ITSM by statuses - WITHOUT OpsDigest GUI.
rem  Just double-click or run from console.
rem
rem  Parameters (optional, override defaults in opsdigest_export.py):
rem    %1 - support group
rem    %2 - statuses (comma-separated)
rem    %3 - created after (DD.MM.YYYY)
rem    %4 - created before (DD.MM.YYYY)
rem
rem  Example (see opsdigest_export.py --help for group/status names):
rem    export_incidents.bat "GROUP_NAME" "STATUS1,STATUS2" "01.11.2026" "10.11.2026"
rem ============================================================================

set "VENV=%LOCALAPPDATA%\OpsDigest\venv"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"

rem --- Check venv ---
if not exist "%VENV%\Scripts\python.exe" (
    echo.
    echo   [!] Virtual environment not found: %VENV%
    echo       Run OpsDigest.bat at least once first.
    echo.
    pause
    exit /b 1
)

set "PY=%VENV%\Scripts\python.exe"

rem --- Check dependencies (playwright, pytesseract, pillow) ---
"%PY%" -c "import playwright, pytesseract, PIL" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [!] Missing dependencies. Installing from requirements.txt ...
    "%PY%" -m pip install -r "%~dp0requirements.txt" --quiet
    if errorlevel 1 (
        echo   [!] Failed to install dependencies. Check network and try again.
        pause
        exit /b 1
    )
    echo   [i] Dependencies installed.
    echo.
)

rem --- Build arguments ---
set "URL=https://itsm.example.com/sm/index.do"
set "CHANNEL=chrome"
set "PROFILE=rpa_profile"
set "ARGS=--url %URL% --channel %CHANNEL% --profile %PROFILE% --no-wait"

if not "%~1"=="" set "ARGS=%ARGS% --group "%~1""
if not "%~2"=="" set "ARGS=%ARGS% --statuses "%~2""
if not "%~3"=="" set "ARGS=%ARGS% --date-after %~3"
if not "%~4"=="" set "ARGS=%ARGS% --date-before %~4"

echo.
echo   === Exporting incidents from ITSM ===
if "%~1"==""  echo   Group:    (default)
if not "%~1"=="" echo   Group:    %~1
if "%~2"==""  echo   Statuses: (default)
if not "%~2"=="" echo   Statuses: %~2
if not "%~3"=="" echo   After:    %~3
if not "%~4"=="" echo   Before:   %~4
echo.
echo   Starting Playwright (Chrome will open)...
echo   Browser window will close automatically when done.
echo.

"%PY%" opsdigest_export.py %ARGS%

echo.
echo   === Done ===
echo   Files saved to: %~dp0opsdigest_output\
pause
