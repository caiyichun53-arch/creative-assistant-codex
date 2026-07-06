@echo off
cd /d "%~dp0"
start "monitor-server (close this window to stop)" cmd /k "python -m scripts.monitor.server --port 8787"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8787/
