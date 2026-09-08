@echo off
REM ============================================================
REM  OWS START - starts the server with the watchdog.
REM  ASCII-only on purpose (see OWS-STOP.bat).
REM
REM  The watchdog keeps the server alive: if it crashes the
REM  server is back within a few seconds. Starting run.py alone
REM  gives you no such protection.
REM ============================================================
setlocal
cd /d "%~dp0"
if not exist "data" mkdir "data"

echo.
echo   OWS - starting...
echo.

REM --- 1) Clear the stop signal, otherwise the watchdog exits at once ---
if exist "data\watchdog.stop" del "data\watchdog.stop" >nul 2>&1
echo   [1/4] stop signal cleared

REM --- 2) venv must exist and must belong to THIS machine ---
if not exist "venv\Scripts\pythonw.exe" (
  echo.
  echo   venv not found. Run OWS-SETUP.bat first.
  echo.
  pause
  exit /b 1
)
"venv\Scripts\python.exe" -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   venv is broken on this machine ^(it was built elsewhere^).
  echo   Run OWS-SETUP.bat to rebuild it.
  echo.
  pause
  exit /b 1
)
echo   [2/4] venv ok

REM --- 3) Already running? Do not start a second one ---
netstat -ano -p TCP | findstr /R /C:"LISTENING" | findstr /C:":5100 " >nul
if not errorlevel 1 (
  echo   [3/4] already running - http://localhost:5100
  echo.
  pause
  exit /b 0
)
echo   [3/4] port 5100 is free

REM --- 4) Re-enable auto-start, then launch the watchdog ---
schtasks /Change /TN "OperationsSystemAutoStart" /ENABLE >nul 2>&1
if errorlevel 1 (
  echo   note: auto-start task not registered - run OWS-SETUP.bat to add it
)
start "" /b "venv\Scripts\pythonw.exe" "watchdog_server.py"

echo   [4/4] watchdog launched, waiting for the server...
set /a TRIES=0
:wait
ping -n 3 127.0.0.1 >nul
set /a TRIES+=1
netstat -ano -p TCP | findstr /R /C:"LISTENING" | findstr /C:":5100 " >nul
if not errorlevel 1 goto ok
if %TRIES% LSS 15 goto wait

echo.
echo   Server did not come up. Check data\logs\ows.log and data\watchdog.log
echo.
pause
exit /b 1

:ok
echo.
echo   OWS is RUNNING.
echo     this PC : http://localhost:5100
echo     network : http://127.0.0.1:5100
echo.
pause
endlocal
