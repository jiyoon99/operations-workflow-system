@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Demo environment missing. Follow README.md first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "scripts\demo_server.py" --no-browser
powershell -NoProfile -ExecutionPolicy Bypass -Command "$url='http://127.0.0.1:5110'; for($i=0;$i -lt 40;$i++){try{Invoke-WebRequest -UseBasicParsing -Uri ($url+'/_demo/info') -TimeoutSec 1 | Out-Null; break}catch{Start-Sleep -Milliseconds 250}}; $paths=@($env:ProgramFiles+'\Google\Chrome\Application\chrome.exe',$env:LOCALAPPDATA+'\Google\Chrome\Application\chrome.exe'); $chrome=$paths | Where-Object {Test-Path -LiteralPath $_} | Select-Object -First 1; if($chrome){Start-Process -FilePath $chrome -ArgumentList $url}else{Start-Process $url}"
exit /b
