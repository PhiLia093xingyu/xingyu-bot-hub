# WeChat hook stack bring-up (WeChat 4.1.10.27 + version.dll hook + OneBot bridge)
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\WeChatBotStack'
$Logs = Join-Path $Root 'logs'
$State = Join-Path $Root 'state'
$Py = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Bridge = Join-Path $Root 'bridge_hook\main.py'
$WeChat = 'C:\Program Files\Tencent\Weixin\Weixin.exe'
$HookDll = 'C:\Program Files\Tencent\Weixin\version.dll'
$AstrRoot = 'C:\AstrBot'
$AstrExe = Join-Path $AstrRoot '.venv\Scripts\astrbot.exe'
$AstrLock = Join-Path $AstrRoot 'astrbot.lock'
New-Item -ItemType Directory -Path $Logs, $State -Force | Out-Null

function Write-Log([string]$msg) {
    Add-Content -Path (Join-Path $Logs 'hook-stack.log') -Value ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) -Encoding UTF8
}
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
function Get-HookStatus {
    try { return Invoke-RestMethod -Uri 'http://127.0.0.1:30001/QueryDB/status' -TimeoutSec 3 } catch { return $null }
}

Write-Log '--- start-hook-stack ---'

# 0) hook DLL must be in place
if (-not (Test-Path $HookDll)) {
    $built = Join-Path $Root 'tmp\wxhook41027_src\WechatHook4.1.10.27-main\x64\Release\version.dll'
    if (Test-Path $built) { Copy-Item $built $HookDll -Force; Write-Log 'restored version.dll from build output' }
    else { Write-Log 'ERROR: version.dll missing and no build output' }
}

# 1) WeChat
if (-not (Get-Process -Name Weixin -ErrorAction SilentlyContinue)) {
    Write-Log 'starting WeChat'
    Start-Process -FilePath $WeChat -ArgumentList 'StartPort=30001 RecvType=2 CallBackURL="http://127.0.0.1:30002/event"'
    Start-Sleep -Seconds 8
}

$hookArgsOk = $false
try {
    $mains = Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' }
    foreach ($mw in $mains) { if ($mw.CommandLine -match 'StartPort=30001') { $hookArgsOk = $true } }
} catch { }
if (-not $hookArgsOk) {
    Write-Log 'WeChat running without hook args (e.g. -autorun); restarting with args'
    Get-Process -Name Weixin -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 4
    Start-Process -FilePath $WeChat -ArgumentList 'StartPort=30001 RecvType=2 CallBackURL="http://127.0.0.1:30002/event"'
    Start-Sleep -Seconds 8
}
# 2) hook HTTP
for ($i = 0; $i -lt 30; $i++) {
    if (Test-Port 30001) { break }
    Start-Sleep -Seconds 2
}
if (-not (Test-Port 30001)) { Write-Log 'WARN: hook HTTP 30001 not listening' }

# 3) wait for login (max 10 min); bridge can still start, callbacks need login
$logged = $false
for ($i = 0; $i -lt 120; $i++) {
    $s = Get-HookStatus
    if ($s -and [int]$s.IsLogin -eq 1) { $logged = $true; break }
    Start-Sleep -Seconds 5
}
Write-Log ('hook login state: ' + $logged)

# 4) AstrBot
if (-not (Test-Port 24874)) {
    $astr = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'astrbot\.exe"?\s+run' }
    if (-not $astr) {
        if (Test-Path $AstrLock) { Remove-Item -LiteralPath $AstrLock -Force; Write-Log 'removed stale astrbot.lock' }
        $tag = Get-Date -Format 'yyyyMMdd_HHmmss'
        Write-Log 'starting AstrBot'
        Start-Process -FilePath $AstrExe -ArgumentList 'run' -WorkingDirectory $AstrRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Logs "astrbot-$tag.out.log") `
            -RedirectStandardError (Join-Path $Logs "astrbot-$tag.err.log")
        for ($i = 0; $i -lt 36; $i++) { Start-Sleep -Seconds 5; if (Test-Port 24874) { break } }
    }
}

# 5) bridge
$hookPid = (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq 30001 } | Select-Object -First 1).OwningProcess
if ($hookPid) { Set-Content -Path (Join-Path $State 'wechat.pid') -Value $hookPid -Encoding ASCII }$running = Get-CimInstance Win32_Process -Filter "(Name='python.exe' or Name='pythonw.exe')" | Where-Object { $_.CommandLine -and $_.CommandLine -match 'bridge_hook\\main\.py' }
if (-not $running) {
    Write-Log 'starting hook bridge'
    Start-Process -FilePath $Py -ArgumentList "`"$Bridge`"" -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs 'hookbridge-stdout.log') `
        -RedirectStandardError (Join-Path $Logs 'hookbridge-stderr.log')
    for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Seconds 3; if (Test-Port 3002) { break } }
}

[ordered]@{
    time = (Get-Date -Format o)
    wechat = [bool](Get-Process -Name Weixin -ErrorAction SilentlyContinue)
    hook_30001 = (Test-Port 30001)
    logged_in = $logged
    bridge_3002 = (Test-Port 3002)
    astrbot_24874 = (Test-Port 24874)
} | ConvertTo-Json | Set-Content -Path (Join-Path $State 'hook-last-start.json') -Encoding UTF8
Write-Log ('done: ' + ((Get-Content (Join-Path $State 'hook-last-start.json') -Raw -Encoding UTF8) -replace '\s+', ' '))


