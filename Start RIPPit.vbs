Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\Start RIPPit.bat"
WshShell.Run Chr(34) & batPath & Chr(34), 0, False
