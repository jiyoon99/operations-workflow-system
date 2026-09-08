@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\demo_server.py" --reset
if errorlevel 1 pause
