@echo off
REM One-time install for "Real browser" / "Render JavaScript" mode.
REM Downloads Playwright + a Chromium browser (~a few hundred MB). Run after setup.bat.
setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    echo [WARNING] No .venv found in this folder.
    echo Run setup.bat FIRST so Playwright installs into the same environment the
    echo crawler runs in. Continuing with the system "python" may not match.
    echo.
    set "PY=python"
)

echo Using Python: "%PY%"
"%PY%" -c "import sys; print('  ->', sys.executable)"
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
"%PY%" -c "import playwright; from playwright.sync_api import sync_playwright; print('  playwright package: OK')"
if errorlevel 1 ( echo [ERROR] Playwright still not importable in this environment. & pause & exit /b 1 )

echo.
echo ============================================================
echo Done. IMPORTANT: start the server with the SAME environment —
echo use start-search.bat (it uses .venv\Scripts\python.exe).
echo Then tick "Real browser (solve challenges)" in the admin page
echo and use the "Test a single URL" box to confirm it works.
echo Real-browser mode opens a visible window, so keep this VM's
echo desktop session open when you use it.
echo ============================================================
pause
