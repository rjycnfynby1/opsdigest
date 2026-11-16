@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv and install requirements-dev.txt first. See docs\DEVELOPMENT.md.
  exit /b 1
)
".venv\Scripts\python.exe" -m pytest %*
exit /b %errorlevel%
