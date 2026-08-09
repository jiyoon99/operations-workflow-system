@echo off
REM ============================================================
REM  HMS STATUS - is it running, and what is it running on?
REM  ASCII-only on purpose (see HMS-STOP.bat).
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo   ==================== HMS STATUS ====================
echo   folder : %~dp0
echo.

REM --- server ---
netstat -ano -p TCP | findstr /R /C:"LISTENING" | findstr /C:":5100 " >nul
if errorlevel 1 (
  echo   server        : STOPPED
) else (
  for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /R /C:"LISTENING" ^| findstr /C:":5100 "') do (
    echo   server        : RUNNING  ^(pid %%P^)  http://localhost:5100
  )
)

REM --- stop signal ---
if exist "data\watchdog.stop" (
  echo   stop signal   : PRESENT - HMS-START.bat will clear it
) else (
  echo   stop signal   : none
)

REM --- auto start ---
schtasks /Query /TN "HalfbookSystemAutoStart" /FO LIST >nul 2>&1
if errorlevel 1 (
  echo   auto-start    : NOT REGISTERED - run HMS-SETUP.bat
) else (
  for /f "tokens=2 delims=:" %%S in ('schtasks /Query /TN "HalfbookSystemAutoStart" /FO LIST ^| findstr /R /C:"^Status" /C:"^Scheduled Task State"') do (
    echo   auto-start    : %%S
  )
)

REM --- venv ---
REM  Keep the checks flat: inside a parenthesised block cmd expands
REM  errorlevel too early, so the branch can report the wrong thing.
if not exist "venv\Scripts\python.exe" goto :novenv
"venv\Scripts\python.exe" -c "import flask" >nul 2>&1
if errorlevel 1 goto :badvenv
echo   venv          : ok
goto :dbcheck
:novenv
echo   venv          : MISSING - run HMS-SETUP.bat
goto :dbcheck
:badvenv
echo   venv          : BROKEN on this machine - run HMS-SETUP.bat
:dbcheck

REM --- database ---
if not exist "data\hms.db" (
  echo   database      : MISSING
) else (
  for %%F in ("data\hms.db") do echo   database      : %%~zF bytes, last change %%~tF
)

REM --- last backup ---
if exist "data\backups\shutdown-latest.db" (
  for %%F in ("data\backups\shutdown-latest.db") do echo   last snapshot : %%~tF
)

echo.
echo   ===================================================
echo.
pause
endlocal
