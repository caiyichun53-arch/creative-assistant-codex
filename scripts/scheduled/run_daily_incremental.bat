@echo off
REM Runs the contract-gated
REM --daily-incremental entrypoint once a day and keeps a dated log so a
REM failed unattended run is visible without digging through Task Scheduler.
cd /d "%~dp0..\.."
if not exist logs mkdir logs
set LOGFILE=logs\daily_incremental_%date:~0,4%%date:~5,2%%date:~8,2%.log
C:\Users\15891\anaconda3\python.exe -m scripts.core.business_data.run_competitor_registration_full --daily-incremental --json >> "%LOGFILE%" 2>&1
