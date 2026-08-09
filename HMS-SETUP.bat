@echo off
REM ============================================================
REM  HMS SETUP - run this ONCE on a new machine.
REM  ASCII-only on purpose (see HMS-STOP.bat).
REM
REM  A venv remembers the python it was built with, so a venv
REM  copied from another PC does not work here. This rebuilds it
REM  and registers the auto-start task with the correct paths.
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo   HMS - first-time setup on this machine
echo   folder: %~dp0
echo.

REM --- 1) find python ---
where python >nul 2>&1
if errorlevel 1 (
  echo   python not found on PATH.
  echo   Install Python 3.12 or newer, tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%V in ('python -c "import sys;print(sys.version.split()[0])"') do echo   [1/4] python %%V found

REM --- 2) rebuild venv ---
if exist "venv" (
  echo   [2/4] removing the old venv ^(it belongs to another PC^)...
  rmdir /s /q "venv"
)
python -m venv venv
if errorlevel 1 (
  echo   failed to create venv.
  pause
  exit /b 1
)
echo   [2/4] venv created

REM --- 3) install packages ---
echo   [3/4] installing packages, this takes a minute...
"venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo   package install failed - check your internet connection.
  pause
  exit /b 1
)
echo   [3/4] packages installed

REM --- 4) register auto-start (survives reboot) ---
schtasks /Create /TN "HalfbookSystemAutoStart" /TR "\"%~dp0venv\Scripts\pythonw.exe\" \"%~dp0watchdog_server.py\"" /SC ONLOGON /RL HIGHEST /F >nul 2>&1
if errorlevel 1 (
  echo   [4/4] could not register auto-start - right-click this file and
  echo         "Run as administrator", or start manually with HMS-START.bat
) else (
  echo   [4/4] auto-start registered
)

echo.
echo   Setup done. Run HMS-START.bat to start the server.
echo.
pause
endlocal
