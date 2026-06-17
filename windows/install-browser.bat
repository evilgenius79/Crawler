@echo off
REM One-time install for "Real browser" / "Render JavaScript" mode.
REM Downloads Playwright + a Chromium browser (~a few hundred MB). Run after setup.bat.
setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Installing Playwright (Python package) ...
"%PY%" -m pip install playwright
if errorlevel 1 ( echo [ERROR] pip install playwright failed. & pause & exit /b 1 )

echo.
echo Downloading the Chromium browser (this can take a few minutes) ...
"%PY%" -m playwright install chromium
if errorlevel 1 ( echo [ERROR] browser download failed. & pause & exit /b 1 )

echo.
echo ============================================================
echo Done. You can now tick "Real browser (solve challenges)" or
echo "Render JavaScript" in the admin page.
echo Note: real-browser mode opens a visible window, so keep this
echo VM's desktop session open when you use it.
echo ============================================================
pause
