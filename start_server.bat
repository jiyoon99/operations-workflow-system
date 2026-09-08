@echo off
REM OWS server launcher. Keep this file ASCII-only:
REM cmd mis-parses UTF-8 batch files that contain non-ASCII text.
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo [OWS] venv not found. Run: python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

if not exist "data" mkdir "data"

REM Already running? Do not start a second instance.
netstat -ano -p TCP | findstr /R /C:"LISTENING" | findstr /C:":5100 " >nul
if not errorlevel 1 (
  echo [OWS] Already running on port 5100 - http://localhost:5100
  exit /b 0
)

start "OWS Server" /min cmd /c ""%~dp0venv\Scripts\python.exe" "%~dp0run.py" >> "%~dp0data\server.log" 2>&1"
echo [OWS] Starting... open http://localhost:5100 (LAN: http://<server-ip>:5100)
