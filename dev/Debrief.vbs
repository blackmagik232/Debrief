' Debrief: opens the Debrief window with no console. Double-click this, or use the desktop shortcut.
Option Explicit
Dim fso, sh, here, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
' this script lives in dev\; the code and .venv are one folder up
here = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pyw = here & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
  MsgBox "Debrief isn't set up yet." & vbCrLf & vbCrLf & "Double-click Setup.bat in the Debrief\dev folder first.", vbExclamation, "Debrief"
  WScript.Quit 1
End If
sh.CurrentDirectory = here
sh.Run """" & pyw & """ -m debrief.desktop", 0, False
