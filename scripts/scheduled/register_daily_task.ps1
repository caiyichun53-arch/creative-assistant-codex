# Registers (or re-registers) the Windows Scheduled Task "CreationAssistant_Daily",
# which the status docs (PHASE_8_AUTHORIZATION_GATE_STATUS.yaml) have carried as a
# disabled placeholder name since GOAL-V0.6.2-PRODUCTION-COMPLETION-01 -- this
# script is what actually makes that name real. Explicit user decision
# (2026-07-08): run once a day at 08:00 local time.
#
# Usage (elevated PowerShell not required -- registers for the current user):
#   powershell -ExecutionPolicy Bypass -File scripts\scheduled\register_daily_task.ps1
#
# To remove: Unregister-ScheduledTask -TaskName "CreationAssistant_Daily" -Confirm:$false

$ErrorActionPreference = "Stop"
$TaskName = "CreationAssistant_Daily"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$BatPath = Join-Path $RepoRoot "scripts\scheduled\run_daily_incremental.bat"
$VbsPath = Join-Path $RepoRoot "scripts\scheduled\run_daily_incremental_hidden.vbs"

if (-not (Test-Path $BatPath)) {
    throw "Launcher not found: $BatPath"
}
if (-not (Test-Path $VbsPath)) {
    throw "Hidden-window wrapper not found: $VbsPath"
}

# Confirmed live 2026-07-09: with the default LogonType (Interactive), a
# visible cmd.exe+conhost.exe pair (PID 18336/4492) appeared on the desktop
# at the exact 08:00:01 trigger time -- Interactive logon runs the task's
# action inside the user's own session, so any console program shows a
# window there no matter what the .bat itself does. Routing the action
# through wscript.exe running run_daily_incremental_hidden.vbs (windowStyle
# 0, wait=True) suppresses that window without needing elevation.
$Action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$VbsPath`"" -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At 8:00AM
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# Best-effort stronger fix: LogonType S4U runs the task in a non-interactive,
# no-desktop session (same account, no password needed), which is the
# textbook correct fix and also covers any future action that isn't routed
# through the hidden-window wrapper. Register-ScheduledTask needs elevated
# rights to change LogonType away from the default Interactive, so this is
# attempted but not required -- if it's denied, we fall back to Interactive
# and rely on the VBS wrapper above, which works either way.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
    Write-Output "Registered scheduled task '$TaskName': daily 08:00, runs $VbsPath -> $BatPath (LogonType=S4U)"
} catch {
    Write-Warning "Could not set LogonType=S4U (needs an elevated PowerShell): $($_.Exception.Message)"
    Write-Warning "Falling back to LogonType=Interactive; the hidden-window wrapper still prevents the console popup."
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
    Write-Output "Registered scheduled task '$TaskName': daily 08:00, runs $VbsPath -> $BatPath (LogonType=Interactive, hidden via VBS)"
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
(Get-ScheduledTask -TaskName $TaskName).Principal | Select-Object UserId, LogonType, RunLevel
(Get-ScheduledTask -TaskName $TaskName).Actions | Select-Object Execute, Arguments
