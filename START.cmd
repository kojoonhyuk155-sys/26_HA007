@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -X utf8 -m venv .venv
  if errorlevel 1 goto failed
  ".venv\Scripts\python.exe" -X utf8 -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -X utf8 run.py
goto end
:failed
echo Setup failed. Check Python installation and network access.
pause
:end
