# Hook stack status report
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\WeChatBotStack'

function Test-Port([int]$port) {
    $ok = $false
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(700)
        if ($ok) { $c.EndConnect($iar) }
        $c.Close()
    } catch { $ok = $false }
    return $ok
}

Write-Host '=== WeChat hook stack ==='
Write-Host ('WeChat process   : ' + $(if (Get-Process -Name Weixin -ErrorAction SilentlyContinue) { 'running' } else { 'DOWN' }))
Write-Host ('Hook HTTP 30001  : ' + $(if (Test-Port 30001) { 'listening' } else { 'DOWN' }))
$login = 'unknown'
if (Test-Port 30001) {
    try { $s = Invoke-RestMethod -Uri 'http://127.0.0.1:30001/QueryDB/status' -TimeoutSec 3; $login = [int]$s.IsLogin } catch { }
}
Write-Host ("Hook IsLogin     : " + $login)
Write-Host ('Bridge 3002      : ' + $(if (Test-Port 3002) { 'listening' } else { 'DOWN' }))
Write-Host ('AstrBot 24874    : ' + $(if (Test-Port 24874) { 'listening' } else { 'DOWN' }))
Write-Host ('AstrBot 24875    : ' + $(if (Test-Port 24875) { 'listening' } else { 'DOWN' }))
Write-Host ''
$sf = Join-Path $Root 'state\hook-status.json'
if (Test-Path $sf) {
    $st = Get-Content $sf -Raw -Encoding UTF8 | ConvertFrom-Json
    $age = [int]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds - [int]$st.ts
    Write-Host ('self_id          : ' + $st.self_id)
    Write-Host ('nickname         : ' + $st.nickname)
    Write-Host ('wxid             : ' + $st.wxid)
    Write-Host ('hook_alive       : ' + $st.hook_alive)
    Write-Host ('reverse connected: ' + $st.reverse_connected)
    Write-Host ('status age       : ' + $age + 's')
}
Write-Host ''
Write-Host '=== autostart ==='
Get-ScheduledTask -TaskName 'WeChatStack-Boot', 'WeChatStack-Watchdog' -ErrorAction SilentlyContinue |
    ForEach-Object { '  {0}: {1}' -f $_.TaskName, $_.State }
