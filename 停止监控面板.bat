@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*scripts.monitor.server*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stopped.
pause
