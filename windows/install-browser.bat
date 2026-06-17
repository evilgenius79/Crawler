@echo off
REM Installs "Real browser" / "Render JavaScript" support (Playwright + Chromium,
REM ~a few hundred MB). Self-sufficient: it creates the .venv and installs the
REM crawler first if you haven't run setup.bat yet.
setlocal
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/ with
    echo "Add python.exe to PATH" ticked, then run this again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo No .venv found - creating it and installing the crawler first ...
    python -m venv .venv
    if errorlevel 1 ( echo [ERROR] Could not create .venv & pause & exit /b 1 )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 ( echo [ERROR] Installing crawler requirements failed. & pause & exit /b 1 )
)

set "PY=.venv\Scripts\python.exe"
echo.
echo Target environment:
"%PY%" -c "import sys; print('  ', sys.executable)"
echo.

echo Installing Playwright (Python package) ...
"%PY%" -m pip install playwright
if errorlevel 1 ( echo [ERROR] pip install playwright failed. & pause & exit /b 1 )

echo.
echo Downloading the Chromium browser (this can take a few minutes) ...
"%PY%" -m playwright install chromium
if errorlevel 1 ( echo [ERROR] browser download failed. & pause & exit /b 1 )

echo.
echo Verifying ...
"%PY%" -c "from playwright.sync_api import sync_playwright; print('  playwright import: OK')"
if errorlevel 1 ( echo [ERROR] Playwright still not importable. & pause & exit /b 1 )

echo.
echo ============================================================
echo Done. Now launch the app with  start-search.bat  (it uses the
echo SAME .venv this installed into). In the admin page tick
echo "Real browser", then use "Test a single URL" to confirm it
echo says  Fetched via: real-browser.
echo Real-browser mode opens a visible window, so keep this VM's
echo desktop session open when you use it.
echo ============================================================
pause
