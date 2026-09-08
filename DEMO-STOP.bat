@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\demo_server.py" --stop
if errorlevel 1 pause
