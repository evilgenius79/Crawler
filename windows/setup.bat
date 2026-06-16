@echo off
REM First-time setup: creates a virtual environment and installs dependencies.
REM Double-click this once after installing Python 3.11+ (with "Add to PATH").
setlocal
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup, then run this again.
    pause
    exit /b 1
)

echo Creating virtual environment (.venv) ...
python -m venv .venv
if errorlevel 1 ( echo [ERROR] Failed to create venv. & pause & exit /b 1 )

echo Installing dependencies ...
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] Dependency install failed. & pause & exit /b 1 )

echo.
echo ============================================================
echo Setup complete.
echo   - Start the search UI:   windows\start-search.bat
echo   - Crawl some sites:      windows\crawl.bat https://example.com
echo ============================================================
pause
