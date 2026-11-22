# Stop the WeChat hook stack (bridge only by default).
param(
    [switch]$IncludeWeChat
)
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\WeChatBotStack'
$Log = Join-Path $Root 'logs\hook-stack.log'

function Write-Log([string]$m) {
    Add-Content -Path $Log -Value ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8
}

$bridge = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'bridge_hook\\main\.py' }
foreach ($p in $bridge) {
    Write-Log ('stopping hook bridge pid ' + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}

if ($IncludeWeChat) {
    Write-Log 'stopping WeChat'
    Stop-Process -Name Weixin -Force -ErrorAction SilentlyContinue
}

Write-Host 'stopped'

