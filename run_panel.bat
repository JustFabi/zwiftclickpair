@echo off
rem Starts the bridge AND the web control panel together, then opens the panel.
rem
rem  - bridge window : BLE + OpenBikeControl (the actual work)
rem  - panel window  : Laravel dashboard at http://localhost:8080
rem    (also reachable from other devices at http://YOUR-PC-IP:8080)
cd /d "%~dp0"

if not exist panel\vendor\ (
    echo First run of the panel needs "composer install" inside panel\.
    echo Run:  cd panel  ^&^&  composer install
    pause
    exit /b 1
)

start "ClickBridge" cmd /k run_clickbridge.bat
start "ClickBridge Panel" cmd /k "cd /d %~dp0panel && php artisan serve --host=0.0.0.0 --port=8080"

rem give the panel a moment, then open a browser
timeout /t 3 /nobreak >nul
start http://localhost:8080
