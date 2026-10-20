@echo off
rem Starts the Discord bot and the stats website (one process). Double-click to run.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo First run: setting up Python packages...
    python -m venv .venv || goto :error
    .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
)
.venv\Scripts\python.exe bot.py
pause
exit /b
:error
echo Setup failed. Is Python 3.10+ installed and on PATH?
pause
