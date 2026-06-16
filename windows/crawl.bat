@echo off
REM Crawls the URL(s) you pass in, following links out to other sites.
REM Usage:  crawl.bat https://example.com [https://another.com ...]
setlocal
cd /d "%~dp0.."

if "%~1"=="" (
    echo Usage: crawl.bat URL [URL2 ...]
    echo Example: crawl.bat https://example.com https://news.ycombinator.com
    pause
    exit /b 1
)

REM ---- Optional: store the index on another drive -------------------------
REM set "CRAWLER_DATA_DIR=D:\crawlerdata"
REM ------------------------------------------------------------------------

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM Broad open-web crawl: follows links to any site. Tune the limits to taste.
"%PY%" -m crawler crawl %* --max-pages 5000 --max-depth 10 --concurrency 15
pause
