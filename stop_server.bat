@echo off
REM HMS server stopper. ASCII-only (see start_server.bat).
cd /d "%~dp0"
if not exist "data" mkdir "data"

REM 1) Tell the watchdog to stop, so it does not restart the server in 5s.
type nul > "data\watchdog.stop"

REM 2) Snapshot before shutdown (safe while running - sqlite online backup).
if exist "venv\Scripts\python.exe" if exist "data\hms.db" (
  "venv\Scripts\python.exe" "scripts\snapshot.py" >nul 2>&1
)

REM 3) Stop the server (taskkill /f skips atexit, so the snapshot is taken above).
if exist "data\hms.pid" (
  set /p HMSPID=<data\hms.pid
  call :kill
  del "data\hms.pid" >nul 2>&1
  echo [HMS] Stopped. Snapshot: data\backups\shutdown-latest.db
  exit /b 0
)

echo [HMS] Not running (no pid file).
exit /b 0

:kill
taskkill /pid %HMSPID% /t /f >nul 2>&1
exit /b 0
