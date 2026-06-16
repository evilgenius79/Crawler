@echo off
REM Crawls one or more sites, following links outward to other sites.
REM   - Double-click it: it will ASK you for the URL(s).
REM   - Or run from a terminal:  crawl.bat https://example.com https://other.com
setlocal
cd /d "%~dp0.."

REM ---- Optional: store the index on another drive -------------------------
REM set "CRAWLER_DATA_DIR=D:\crawlerdata"
REM ------------------------------------------------------------------------

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM Use URLs passed on the command line, otherwise prompt for them.
set "SEEDS=%*"
if "%SEEDS%"=="" (
    echo.
    echo Enter one or more URLs to crawl, separated by spaces.
    echo Example: https://example.com https://news.ycombinator.com
    echo.
    set /p "SEEDS=URLs: "
)

if "%SEEDS%"=="" (
    echo No URLs entered. Nothing to do.
    pause
    exit /b 1
)

echo.
echo Crawling: %SEEDS%
echo (This can take a while. Press Ctrl+C to stop early - progress is saved.)
echo.
"%PY%" -m crawler crawl %SEEDS% --max-pages 5000 --max-depth 10 --concurrency 15

echo.
echo ===== Crawl finished. You can now search via start-search.bat =====
pause
