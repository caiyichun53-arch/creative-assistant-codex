' Start Feishu listener hidden (for logon autostart).
' Set CREATION_PYTHON to a full python.exe/pythonw.exe path when PATH is not enough.
' ASCII-only on purpose: .vbs is parsed as system ANSI; non-ASCII comments break wscript.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projectRoot = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonExe = sh.ExpandEnvironmentStrings("%CREATION_PYTHON%")
If pythonExe = "%CREATION_PYTHON%" Or pythonExe = "" Then
  pythonExe = "python"
End If

cmd = "cmd /c cd /d " & Quote(projectRoot) & " && " & _
      Quote(pythonExe) & " scripts\feishu\listener.py >> logs\listener_stdout.log 2>&1"
sh.Run cmd, 0, True    ' 0 = hidden window, True = WAIT (keep wscript alive so Task Scheduler
                       ' does not reap the python child when the launcher exits)

Function Quote(value)
  Quote = """" & value & """"
End Function
