# WeChat bot stack status report
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\WeChatBotStack'
$AstrRoot = 'C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378'

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

$wechat = Get-Process -Name Weixin -ErrorAction SilentlyContinue
$bridge = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'bridge\\main\.py' }
$status = $null
$sf = Join-Path $Root 'state\status.json'
if (Test-Path $sf) { try { $status = Get-Content $sf -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $status = $null } }

Write-Host '=== WeChat bot stack status ==='
Write-Host ('WeChat process      : ' + $(if ($wechat) { 'running (' + (($wechat | ForEach-Object { $_.Id }) -join ',') + ')' } else { 'DOWN' }))
Write-Host ('Bridge process      : ' + $(if ($bridge) { 'running (' + (($bridge | ForEach-Object { $_.ProcessId }) -join ',') + ')' } else { 'DOWN' }))
Write-Host ('OneBot WS server    : ' + $(if (Test-Port 3002) { 'listening on 3002' } else { 'DOWN' }))
Write-Host ('AstrBot QQ platform : ' + $(if (Test-Port 24874) { 'listening on 24874' } else { 'DOWN' }))
Write-Host ('AstrBot WX platform : ' + $(if (Test-Port 24875) { 'listening on 24875' } else { 'DOWN' }))
Write-Host ('AstrBot dashboard   : ' + $(if (Test-Port 6185) { 'listening on 6185' } else { 'DOWN' }))
if ($status) {
    $age = [int]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds - [int]$status.ts
    Write-Host ''
    Write-Host ('self_id             : ' + $status.self_id)
    Write-Host ('nickname            : ' + $status.nickname)
    Write-Host ('wxid                : ' + $status.wxid)
    Write-Host ('listener            : ' + $status.listener)
    Write-Host ('reverse connected   : ' + $status.reverse_connected)
    Write-Host ('status age          : ' + $age + 's')
    Write-Host ('last inbound event  : ' + $(if ($status.last_event_ts -gt 0) { (Get-Date '1970-01-01').AddSeconds([int]$status.last_event_ts).ToString('yyyy-MM-dd HH:mm:ss') } else { 'none since start' }))
}
Write-Host ''
Write-Host '=== AstrBot platform config ==='
$cfgPath = Join-Path $AstrRoot 'data\cmd_config.json'
if (Test-Path $cfgPath) {
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg.platform | ForEach-Object { '  {0} | {1} | enable={2} | port={3}' -f $_.id, $_.type, $_.enable, $_.ws_reverse_port }
}
Write-Host ''
Write-Host '=== autostart tasks ==='
Get-ScheduledTask -TaskName 'WeChatStack-Boot', 'WeChatStack-Watchdog' -ErrorAction SilentlyContinue |
    ForEach-Object { '  {0}: {1}' -f $_.TaskName, $_.State }

