@echo off
REM Launches the search web UI at http://localhost:8000 (and on your LAN via the
REM VM's IP). Leave this window open while you want search available.
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

echo Starting PersonalSearch UI on http://localhost:8000
echo (Reachable from other PCs at http://THIS-VM-IP:8000 once the firewall allows port 8000.)
echo Press Ctrl+C to stop.
"%PY%" -m crawler serve --host 0.0.0.0 --port 8000
pause
