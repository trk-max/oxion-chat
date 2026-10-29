@echo off
rem My Only Chat launcher: double-click or run "run.cmd" from cmd.
rem Optional: run.cmd 8080  (custom port, default 8000)
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || (echo Python not found. Install Python 3 first. & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
)

echo Starting My Only Chat on http://127.0.0.1:%PORT%  (Ctrl+C to stop)
start "" "http://127.0.0.1:%PORT%"
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port %PORT%
pause
