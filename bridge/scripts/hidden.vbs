' Launch a PowerShell script completely hidden (no console window flash).
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count >= 1 Then
  script = WScript.Arguments(0)
  cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & script & """"
  sh.Run cmd, 0, False
End If