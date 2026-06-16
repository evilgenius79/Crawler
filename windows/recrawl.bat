@echo off
REM Incremental update: re-fetches anything indexed more than 1 day ago and keeps
REM following links. Meant to be run by Task Scheduler (no pause), but you can
REM also double-click it. Edit --older-than-days to change freshness.
setlocal
cd /d "%~dp0.."

REM ---- Optional: match the data location used by your crawls --------------
REM set "CRAWLER_DATA_DIR=D:\crawlerdata"
REM ------------------------------------------------------------------------

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -m crawler recrawl --older-than-days 1 --max-pages 20000 --max-depth 10
