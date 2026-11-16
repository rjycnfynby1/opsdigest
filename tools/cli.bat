@echo off
chcp 65001 >nul
rem Командная строка OpsDigest (для опытных): cli.bat digest --period week, cli.bat ask "вопрос", cli.bat demo, cli.bat --help
rem Работает с той же рабочей папкой, что и окно. Сначала один раз запустите OpsDigest.bat.
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONPATH=%CD%"
set "PY=%LOCALAPPDATA%\OpsDigest\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Сначала запустите OpsDigest.bat — он подготовит окружение.
  pause
  exit /b 1
)
if "%~1"=="" (
  "%PY%" -m opsdigest --help
  pause
  exit /b 0
)
"%PY%" -m opsdigest %*
