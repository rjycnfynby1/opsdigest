@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
rem Сборка OpsDigest-voice.zip (пакеты + модель для голосового ввода) на компьютере с интернетом.
where py >/dev/null 2>nul
if %errorlevel%==0 (py -3 tools\voice_pack.py %*) else (python tools\voice_pack.py %*)
pause
