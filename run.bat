@echo off
setlocal EnableDelayedExpansion

chcp 65001 >nul 2>&1
if errorlevel 1 (
    echo [WARN] chcp 65001 не сработал, продолжаем в системной кодировке.
)

cd /d "%~dp0"

:: --- Конфигурация (меняй здесь) ---
set "PORTAL_URL=https://itsm.example.com/"
set "BROWSER=chrome"
:: -----------------------------------

:menu
cls
echo ============================================
echo   ITSM RPA - Portal Clicker (iFrame)
echo ============================================
echo.
echo   URL:    %PORTAL_URL%
echo   Браузер: %BROWSER%
echo.
echo   [1] Быстрый dry-run (собрать данные, без действий)
echo   [2] Scout - разведка структуры портала
echo   [3] Полный запуск (С ДЕЙСТВИЯМИ!)
echo   [4] Сеть - dry-run + лог запросов
echo   [5] Поиск по полям формы
echo   [6] Mock-тесты (локально, без портала)
echo   [7] Проверить результаты (tickets.json + логи)
echo   [8] Открыть папку ошибок (rpa_errors)
echo   [9] Установить зависимости
echo   [0] Выход
echo.
set /p choice=Выбор [0-9]: 

if "%choice%"=="1" goto dryrun
if "%choice%"=="2" goto scout
if "%choice%"=="3" goto full
if "%choice%"=="4" goto network
if "%choice%"=="5" goto search
if "%choice%"=="6" goto mock
if "%choice%"=="7" goto check
if "%choice%"=="8" goto errors
if "%choice%"=="9" goto install
if "%choice%"=="0" goto end

echo Неизвестный выбор.
timeout /t 2 >nul
goto menu

:: --- Быстрый dry-run ---
:dryrun
echo.
echo Запуск dry-run: %PORTAL_URL% (%BROWSER%)
echo Результат: tickets.json
echo.
python example_scenario.py --url %PORTAL_URL% --channel %BROWSER% --dry-run
echo.
echo ==================================
echo Завершено. Проверить результат: [7]
echo ==================================
pause
goto menu

:: --- Scout ---
:scout
echo.
echo Scout: откроется браузер, войдите в портал,
echo откройте нужную очередь, затем нажмите Enter здесь.
echo.
python itsm_rpa.py scout --url %PORTAL_URL% --channel %BROWSER%
echo.
pause
goto menu

:: --- Полный запуск ---
:full
echo.
echo  ВНИМАНИЕ: будут ВЫПОЛНЯТЬСЯ ДЕЙСТВИЯ!
echo  (принять в работу, назначить, переводить)
echo.
set /p confirm=Подтвердить? (y/n): 
if /i not "%confirm%"=="y" goto menu
echo.
python example_scenario.py --url %PORTAL_URL% --channel %BROWSER%
echo.
pause
goto menu

:: --- Сеть ---
:network
echo.
echo Dry-run + лог сетевых запросов.
echo.
python example_scenario.py --url %PORTAL_URL% --channel %BROWSER% --dry-run --record-network
echo.
pause
goto menu

:: --- Поиск ---
:search
echo.
set /p GROUP=Группа поддержки (Enter = пропустить): 
set /p DATE_AFTER=Дата после (дд.мм.гггг, Enter = пропустить): 
set ARGS=--url %PORTAL_URL% --channel %BROWSER% --search
if not "%GROUP%"=="" set ARGS=%ARGS% --group "%GROUP%"
if not "%DATE_AFTER%"=="" set ARGS=%ARGS% --date-after "%DATE_AFTER% 00:00:00"
echo.
echo Запуск поиска: %ARGS%
echo.
python example_scenario.py %ARGS%
echo.
pause
goto menu

:: --- Mock ---
:mock
echo.
echo Запуск mock-тестов (локально, без портала).
echo.
python tests\run_mock_tests.py --show --runs 1
echo.
pause
goto menu

:: --- Проверить результаты ---
:check
echo.
echo ==================================
echo  tickets.json:
echo ==================================
if exist "tickets.json" (
    for %%F in (tickets.json) do echo   Размер: %%~zF байт
    echo.
    echo   Содержимое (первые 50 строк):
    echo --------------------------------
    more +0 tickets.json | findstr /n "^" | findstr /b "1-50:"
    echo --------------------------------
) else (
    echo   Файл tickets.json НЕ НАЙДЕН (dry-run ещё не выполнялся).
)
echo.
echo ==================================
echo  Последний лог (run_debug.log):
echo ==================================
if exist "run_debug.log" (
    echo   Последние 20 строк:
    echo --------------------------------
    powershell -Command "Get-Content run_debug.log -Tail 20"
    echo --------------------------------
) else (
    echo   Лог не найден.
)
echo.
echo ==================================
echo  Ошибки (rpa_errors/):
echo ==================================
if exist "rpa_errors" (
    dir /b /o-d rpa_errors\*.png 2>nul | findstr /n "^" | findstr /b "1-5:"
    if errorlevel 1 echo   Скриншотов ошибок нет.
) else (
    echo   Папка rpa_errors не найдена.
)
echo.
pause
goto menu

:: --- Ошибки ---
:errors
if exist "rpa_errors" (
    echo Открываю rpa_errors...
    explorer rpa_errors
) else (
    echo Папка rpa_errors не найдена.
)
timeout /t 3 >nul
goto menu

:: --- Установка ---
:install
echo.
echo Установка зависимостей...
pip install -r requirements.txt
echo.
echo Проверка Playwright:
python -m playwright install --dry-run
echo.
echo Готово!
pause
goto menu

:end
echo Пока!
endlocal
