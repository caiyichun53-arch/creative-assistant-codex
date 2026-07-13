' Launches run_daily_incremental.bat with a fully hidden window (style 0) and
' waits for it to finish before returning, so Task Scheduler still reports the
' real exit status/duration instead of "finished" the instant the wrapper
' starts. This is the standard no-admin-required way to stop a scheduled .bat
' from flashing a visible cmd.exe/conhost.exe window: Task Scheduler's default
' Interactive logon type runs the action inside the user's desktop session, so
' any console program shows a window there regardless of what the .bat itself
' does -- WScript.Shell.Run's windowStyle argument is what actually suppresses
' it, independent of logon type.
Dim fso, shell, scriptDir, batPath
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\run_daily_incremental.bat"
shell.Run """" & batPath & """", 0, True
