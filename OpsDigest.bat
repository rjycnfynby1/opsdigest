@echo off
chcp 65001 >nul
title OpsDigest
cd /d "%~dp0"
rem ============================================================================
rem  OpsDigest — запуск. Просто откройте этот файл двойным щелчком.
rem  Первый раз: сам найдёт или скачает Python, поставит всё нужное и откроет окно.
rem  Всё служебное — в %LOCALAPPDATA%\OpsDigest (не в этой папке и не в OneDrive).
rem ============================================================================
set "RT=%LOCALAPPDATA%\OpsDigest"
set "VENV=%RT%\venv"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"
set "PYURL=https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe"
if not exist "%RT%\logs" mkdir "%RT%\logs"
rem журнал запуска: только шаги и коды, без путей и имён — его можно переслать
set "LOGF=%RT%\logs\launcher.log"
call :log ==== запуск

if exist "%VENV%\Scripts\python.exe" (
  "%VENV%\Scripts\python.exe" -c "import sys" >nul 2>&1
  if not errorlevel 1 goto run
  call :log окружение повреждено, пересоздаю
  echo   Окружение повреждено ^(например, удалён Python^) — пересоздаю...
  rmdir /s /q "%VENV%"
)

echo.
echo   OpsDigest: первый запуск — готовлю всё необходимое (обычно 1-3 минуты).
echo.
call :findpy
if not defined PY call :getpy
if not defined PY goto nopy
call :log python найден: %PYKIND%
echo   Python: %PY%
echo   Создаю окружение программы...
%PY% -m venv "%VENV%" >>"%LOGF%" 2>&1
if not exist "%VENV%\Scripts\python.exe" goto venvfail
call :log окружение создано

:run
"%VENV%\Scripts\python.exe" -m opsdigest.bootstrap
if errorlevel 1 goto depsfail
call :log пакеты в порядке, открываю окно
rem запускает сторожа без консоли (он держит программу запущенной и поднимает после сбоя), ждёт ответа и открывает
rem окно; эта консоль закроется сама. Программа работает в фоне, пока её не закроют кнопкой «Выход» в окне.
"%VENV%\Scripts\python.exe" -m opsdigest.app
if errorlevel 1 goto startfail
exit /b 0

:startfail
call :log сервер не ответил при запуске
call :report
echo.
echo   Окно можно закрыть. Перешлите отчёт и попробуйте запустить OpsDigest.bat ещё раз.
pause
exit /b 1

:log
>>"%LOGF%" echo %date% %time:~0,8% %*
exit /b 0

rem ---------------------------------------------------------------- отчёт об ошибке для пересылки
:report
echo.
echo   Собираю отчёт об ошибке (без личных данных) — его можно переслать разработчику...
if exist "%VENV%\Scripts\python.exe" (
  "%VENV%\Scripts\python.exe" -m opsdigest.report --open
  exit /b 0
)
if defined PY (
  %PY% -m opsdigest.report --open
  exit /b 0
)
echo   Python недоступен, поэтому отчёта не будет. Перешлите файл журнала запуска:
echo   %LOGF%
start "" explorer /select,"%LOGF%"
exit /b 0

rem ---------------------------------------------------------------- поиск Python 3.10+
:findpy
set "PY="
set "PYKIND="
if exist "%RT%\python\python.exe" (
  set PY="%RT%\python\python.exe"
  set "PYKIND=скачанный ранее"
  exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if not errorlevel 1 (
    set "PY=py -3"
    set "PYKIND=py-лаунчер"
  )
)
if defined PY exit /b 0
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PY=python"
  set "PYKIND=python из PATH"
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

rem ---------------------------------------------------------------- скачать Python (без прав администратора)
:getpy
echo   Python не найден. Скачиваю Python 3.13 с python.org (около 30 МБ)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; try { Invoke-WebRequest -Uri $env:PYURL -OutFile (Join-Path $env:TEMP 'opsdigest-python.exe') -UseBasicParsing; exit 0 } catch { Write-Host ('   ' + $_.Exception.Message); exit 1 }"
if errorlevel 1 (
  call :log скачать Python не удалось
  exit /b 0
)
call :log Python скачан, устанавливаю
echo   Устанавливаю Python только для OpsDigest — без прав администратора, в %RT%\python ...
"%TEMP%\opsdigest-python.exe" /quiet InstallAllUsers=0 TargetDir="%RT%\python" Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 Shortcuts=0 AssociateFiles=0 PrependPath=0
del "%TEMP%\opsdigest-python.exe" >nul 2>nul
if exist "%RT%\python\python.exe" (
  set PY="%RT%\python\python.exe"
  set "PYKIND=скачанный сейчас"
) else (
  call :log установка Python не удалась
)
exit /b 0

:nopy
call :log Python не найден и не установлен
echo.
echo   [!] Не получилось найти или скачать Python.
echo       Установите Python 3.10 или новее (python.org или корпоративный магазин приложений)
echo       и снова запустите OpsDigest.bat.
start "" "https://www.python.org/downloads/windows/"
call :report
pause
exit /b 1

:venvfail
call :log не удалось создать окружение
echo.
echo   [!] Не удалось создать окружение в %VENV%
call :report
pause
exit /b 1

:depsfail
call :log пакеты не установились
call :report
echo.
echo   Окно можно закрыть. Исправьте причину выше и снова запустите OpsDigest.bat.
pause
exit /b 1
