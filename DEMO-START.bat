@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Demo environment missing. Follow README.md first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "scripts\demo_server.py"
if errorlevel 1 pause
