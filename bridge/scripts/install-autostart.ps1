# Register WeChat bot stack autostart (per-user, no admin required).
$ErrorActionPreference = 'Stop'
$Root = 'C:\ProgramData\WeChatBotStack'
$Scripts = Join-Path $Root 'scripts'
$Start = Join-Path $Scripts 'start-stack.ps1'
$Watch = Join-Path $Scripts 'watchdog.ps1'
$User = "$env:USERDOMAIN\$env:USERNAME"

function New-Action([string]$file) {
    New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $file) `
        -WorkingDirectory $Root
}

try {
    $bootTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $bootTrigger.Delay = 'PT60S'
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $bootSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    Register-ScheduledTask -TaskName 'WeChatStack-Boot' -Action (New-Action $Start) -Trigger $bootTrigger `
        -Principal $principal -Settings $bootSettings -Force `
        -Description 'Start WeChat + WeChat OneBot bridge + AstrBot at logon' | Out-Null
    Write-Host 'registered WeChatStack-Boot'

    $watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(3) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue)
    $watchSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName 'WeChatStack-Watchdog' -Action (New-Action $Watch) -Trigger $watchTrigger `
        -Principal $principal -Settings $watchSettings -Force `
        -Description 'Watchdog: restart WeChat/AstrBot/bridge if down' | Out-Null
    Write-Host 'registered WeChatStack-Watchdog'
} catch {
    Write-Host ('Register-ScheduledTask failed: {0}' -f $_.Exception.Message)
    Write-Host 'falling back to schtasks.exe'
    $startArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $Start
    $watchArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $Watch
    & schtasks.exe /Create /TN 'WeChatStack-Boot' /SC ONLOGON /DELAY 0001:00 /TR ('powershell.exe {0}' -f $startArgs) /F | Out-Null
    & schtasks.exe /Create /TN 'WeChatStack-Watchdog' /SC MINUTE /MO 5 /TR ('powershell.exe {0}' -f $watchArgs) /F | Out-Null
    Write-Host 'registered via schtasks'
}

Get-ScheduledTask -TaskName 'WeChatStack-Boot', 'WeChatStack-Watchdog' -ErrorAction SilentlyContinue |
    Select-Object TaskName, State | Format-Table -AutoSize
Get-ScheduledTaskInfo -TaskName 'WeChatStack-Boot' -ErrorAction SilentlyContinue | Format-List TaskName, LastRunTime, LastTaskResult
