# WeChat bot stack: bring-up script (idempotent, no admin required)
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\WeChatBotStack'
$Logs = Join-Path $Root 'logs'
$State = Join-Path $Root 'state'
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Bridge = Join-Path $Root 'bridge\main.py'
$AstrRoot = 'C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378'
$AstrExe = Join-Path $AstrRoot '.venv\Scripts\astrbot.exe'
$AstrLock = Join-Path $AstrRoot 'astrbot.lock'
$WeChatExe = 'C:\Program Files\Tencent\Weixin\Weixin.exe'
New-Item -ItemType Directory -Path $Logs, $State -Force | Out-Null

function Write-Log([string]$msg) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path (Join-Path $Logs 'stack.log') -Value $line -Encoding UTF8
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

function Get-ProcByCmd([string]$pattern) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern }
}

function Get-AstrBotProc {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'astrbot\.exe"?\s+run' }
}

Write-Log '--- start-stack invoked ---'

# 1) WeChat must be running (it also has its own HKCU Run entry)
if (-not (Get-Process -Name Weixin -ErrorAction SilentlyContinue)) {
    Write-Log 'WeChat not running, launching Weixin.exe'
    Start-Process -FilePath $WeChatExe
    Start-Sleep -Seconds 8
} else {
    Write-Log 'WeChat process present'
}

# 2) AstrBot (QQ bot + WeChat bot platform share this AstrBot instance)
if (-not (Test-Port 24874)) {
    $astr = Get-AstrBotProc
    if (-not $astr) {
        if (Test-Path $AstrLock) { Remove-Item -LiteralPath $AstrLock -Force -ErrorAction SilentlyContinue; Write-Log 'removed stale astrbot.lock' }
        $tag = Get-Date -Format 'yyyyMMdd_HHmmss'
        Write-Log 'starting AstrBot'
        Start-Process -FilePath $AstrExe -ArgumentList 'run' -WorkingDirectory $AstrRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Logs "astrbot-$tag.out.log") `
            -RedirectStandardError (Join-Path $Logs "astrbot-$tag.err.log")
        for ($i = 0; $i -lt 36; $i++) { Start-Sleep -Seconds 5; if (Test-Port 24874) { break } }
    }
    if (Test-Port 24874) { Write-Log 'AstrBot is listening on 24874' } else { Write-Log 'WARN: AstrBot not listening on 24874 yet' }
} else {
    Write-Log 'AstrBot already listening on 24874'
}

# 3) WeChat OneBot bridge
$running = Get-ProcByCmd 'bridge\\main\.py'
if (-not $running) {
    Write-Log 'starting WeChat->OneBot bridge'
    Start-Process -FilePath $Py -ArgumentList "`"$Bridge`"" -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs 'bridge-stdout.log') `
        -RedirectStandardError (Join-Path $Logs 'bridge-stderr.log')
    for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 4; if (Test-Port 3002) { break } }
    if (Test-Port 3002) { Write-Log 'bridge listening on 3002' } else { Write-Log 'WARN: bridge not listening on 3002 yet' }
} else {
    Write-Log ('bridge already running: pid ' + (($running | ForEach-Object { $_.ProcessId }) -join ','))
}

# 4) summary
$summary = [ordered]@{
    time = (Get-Date -Format o)
    wechat = [bool](Get-Process -Name Weixin -ErrorAction SilentlyContinue)
    astrbot_24874 = (Test-Port 24874)
    astrbot_24875 = (Test-Port 24875)
    bridge_3002 = (Test-Port 3002)
}
$summary | ConvertTo-Json | Set-Content -Path (Join-Path $State 'last-start.json') -Encoding UTF8
Write-Log ('summary: ' + ($summary | ConvertTo-Json -Compress))

