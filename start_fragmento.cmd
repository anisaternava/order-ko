@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo First complete the setup in README.md.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "app.py"
