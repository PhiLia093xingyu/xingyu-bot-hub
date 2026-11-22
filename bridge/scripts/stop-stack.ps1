# Stop the WeChat OneBot bridge (optionally AstrBot). WeChat itself keeps running.
param(
    [switch]$IncludeAstrBot
)
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\WeChatBotStack'
$Log = Join-Path $Root 'logs\stack.log'

function Write-Log([string]$msg) {
    Add-Content -Path $Log -Value ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) -Encoding UTF8
}

$bridge = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'bridge\\main\.py' }
foreach ($p in $bridge) {
    Write-Log ('stopping bridge pid ' + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

if ($IncludeAstrBot) {
    $astr = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'astrbot\.exe"?\s+run' }
    foreach ($p in $astr) {
        Write-Log ('stopping AstrBot pid ' + $p.ProcessId)
        & taskkill /F /T /PID $p.ProcessId 2>&1 | Out-Null
    }
}

Write-Host 'stopped'
