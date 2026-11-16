@echo off
setlocal
chcp 65001 >nul 2>&1
title OpsDigest
cd /d "%~dp0"

rem ASCII-only launcher so cmd.exe works on systems with legacy code pages.
set "RT=%LOCALAPPDATA%\OpsDigest"
set "VENV=%RT%\venv"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"
set "PYURL=https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe"
if not exist "%RT%\logs" mkdir "%RT%\logs"
set "LOGF=%RT%\logs\launcher.log"
call :log Launcher started

if exist "%VENV%\Scripts\python.exe" (
  "%VENV%\Scripts\python.exe" -c "import sys" >nul 2>&1
  if not errorlevel 1 goto run
  call :log Existing environment is invalid; recreating it
  echo The Python environment is invalid. Recreating it...
  rmdir /s /q "%VENV%"
)

echo.
echo First run: preparing OpsDigest. This usually takes 1-3 minutes.
echo.
call :findpy
if not defined PY call :getpy
if not defined PY goto nopy
call :log Python found: %PYKIND%
echo Python: %PY%
echo Creating the application environment...
%PY% -m venv "%VENV%" >>"%LOGF%" 2>&1
if not exist "%VENV%\Scripts\python.exe" goto venvfail
call :log Environment created

:run
"%VENV%\Scripts\python.exe" -m opsdigest.bootstrap
if errorlevel 1 goto depsfail
call :log Dependencies are ready; starting application
"%VENV%\Scripts\python.exe" -m opsdigest.app
if errorlevel 1 goto startfail
exit /b 0

:startfail
call :log Application server failed to start
call :report
echo.
echo Close this window after reviewing the report, then try again.
pause
exit /b 1

:log
>>"%LOGF%" echo %date% %time:~0,8% %*
exit /b 0

:report
echo.
echo Creating a diagnostic report...
if exist "%VENV%\Scripts\python.exe" (
  "%VENV%\Scripts\python.exe" -m opsdigest.report --open
  exit /b 0
)
if defined PY (
  %PY% -m opsdigest.report --open
  exit /b 0
)
echo Python is unavailable. Send this launcher log to support:
echo %LOGF%
start "" explorer /select,"%LOGF%"
exit /b 0

:findpy
set "PY="
set "PYKIND="
if exist "%RT%\python\python.exe" (
  set PY="%RT%\python\python.exe"
  set "PYKIND=previously downloaded"
  exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if not errorlevel 1 (
    set "PY=py -3"
    set "PYKIND=Python launcher"
  )
)
if defined PY exit /b 0
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PY=python"
  set "PYKIND=Python from PATH"
)
if defined PY exit /b 0
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%~D\python.exe" (
    "%%~D\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
      set PY="%%~D\python.exe"
      set "PYKIND=Programs\Python"
    )
  )
)
if defined PY exit /b 0
if exist "%USERPROFILE%\miniconda3\python.exe" (
  set PY="%USERPROFILE%\miniconda3\python.exe"
  set "PYKIND=miniconda"
)
exit /b 0

:getpy
echo Python 3.10+ was not found. Downloading Python 3.13 from python.org...
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; try { Invoke-WebRequest -Uri $env:PYURL -OutFile (Join-Path $env:TEMP 'opsdigest-python.exe') -UseBasicParsing; exit 0 } catch { Write-Host ('   ' + $_.Exception.Message); exit 1 }"
if errorlevel 1 (
  call :log Python download failed
  exit /b 0
)
call :log Python downloaded; installing
echo Installing Python for OpsDigest in %RT% (no administrator rights required)...
"%TEMP%\opsdigest-python.exe" /quiet InstallAllUsers=0 TargetDir="%RT%\python" Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 Shortcuts=0 AssociateFiles=0 PrependPath=0
del "%TEMP%\opsdigest-python.exe" >nul 2>nul
if exist "%RT%\python\python.exe" (
  set PY="%RT%\python\python.exe"
  set "PYKIND=downloaded for this run"
) else (
  call :log Python installation failed
)
exit /b 0

:nopy
call :log Python was not found or installed
echo.
echo Python could not be found or downloaded.
echo Install Python 3.10 or newer, then run OpsDigest.bat again.
start "" "https://www.python.org/downloads/windows/"
call :report
pause
exit /b 1

:venvfail
call :log Failed to create the Python environment
echo Could not create the environment in %VENV%.
call :report
pause
exit /b 1

:depsfail
call :log Dependency installation failed
call :report
echo.
echo Fix the reported issue, then run OpsDigest.bat again.
pause
exit /b 1
