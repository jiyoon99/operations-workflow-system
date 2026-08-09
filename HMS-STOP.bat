@echo off
REM ============================================================
REM  HMS STOP - stops the server for real.
REM  ASCII-only on purpose: cmd mis-parses batch files that
REM  contain non-ASCII text and the whole file silently fails.
REM
REM  Why three steps: the watchdog restarts the server within
REM  a few seconds, and the Task Scheduler restarts the watchdog.
REM  Killing the process alone is not enough.
REM ============================================================
setlocal
cd /d "%~dp0"
if not exist "data" mkdir "data"

echo.
echo   HMS - stopping...
echo.

REM --- 1) Tell the watchdog to stop (it exits with the server) ---
type nul > "data\watchdog.stop"
echo   [1/4] watchdog stop signal written

REM --- 2) Stop the scheduled task so it does not come back ---
schtasks /End /TN "HalfbookSystemAutoStart" >nul 2>&1
schtasks /Change /TN "HalfbookSystemAutoStart" /DISABLE >nul 2>&1
echo   [2/4] auto-start task disabled

REM --- 3) Snapshot the database while it is still healthy ---
REM  Note: "if exist A if exist B (..) else (..)" does NOT work in cmd -
REM  a nested if swallows the else. Keep the checks flat.
REM  Do not judge success by "the file exists" - an OLD snapshot from a
REM  previous run is still there and would be reported as a fresh save.
REM  Use the exit code of snapshot.py instead.
set "SNAP=no"
if exist "venv\Scripts\python.exe" if exist "data\hms.db" set "SNAP=yes"
if "%SNAP%"=="no" goto :nosnap
"venv\Scripts\python.exe" "scripts\snapshot.py" >nul 2>&1
if errorlevel 1 goto :snapfail
echo   [3/4] snapshot saved: data\backups\shutdown-latest.db
goto :killstep
:snapfail
echo   [3/4] snapshot FAILED - the venv does not work on this PC
echo         run HMS-SETUP.bat, the database itself is untouched
goto :killstep
:nosnap
echo   [3/4] snapshot skipped ^(venv or database missing^)
:killstep

REM --- 4) Give the watchdog a moment, then kill what is left ---
ping -n 6 127.0.0.1 >nul
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /R /C:"LISTENING" ^| findstr /C:":5100 "') do (
  taskkill /pid %%P /t /f >nul 2>&1
)
if exist "data\hms.pid" del "data\hms.pid" >nul 2>&1
ping -n 3 127.0.0.1 >nul

REM --- Verify ---
netstat -ano -p TCP | findstr /R /C:"LISTENING" | findstr /C:":5100 " >nul
if errorlevel 1 (
  echo   [4/4] port 5100 is free
  echo.
  echo   HMS is STOPPED. Run HMS-START.bat to bring it back.
) else (
  echo   [4/4] WARNING: something is still listening on port 5100
  echo.
  echo   Run this file once more, or check Task Manager for python.exe
)
echo.
pause
endlocal
