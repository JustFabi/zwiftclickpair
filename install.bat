@echo off
rem First-time setup: creates a virtualenv and installs dependencies.
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv || goto :err
)
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto :err
echo.
echo Setup complete. Start the bridge with run_clickbridge.bat
pause
exit /b 0

:err
echo.
echo Setup FAILED - make sure Python 3.12+ is on PATH (python.org).
pause
exit /b 1
