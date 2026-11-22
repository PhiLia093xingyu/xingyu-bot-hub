# Watchdog for the hook stack: WeChat+hook, bridge, AstrBot.
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\WeChatBotStack'
$Logs = Join-Path $Root 'logs'
$State = Join-Path $Root 'state'
$Log = Join-Path $Logs 'hook-watchdog.log'
$StateFile = Join-Path $State 'hook-watchdog-state.json'
$Py = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Bridge = Join-Path $Root 'bridge_hook\main.py'
$WeChat = 'C:\Program Files\Tencent\Weixin\Weixin.exe'
$Stamp = Join-Path $State 'hook-watchdog.lastrestart'
New-Item -ItemType Directory -Path $Logs, $State -Force | Out-Null

function Write-Log([string]$m) {
    Add-Content -Path $Log -Value ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8
    if ((Get-Item $Log).Length -gt 2MB) { Set-Content -Path $Log -Value (Get-Content $Log -Tail 800) -Encoding UTF8 }
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

$mutex = New-Object System.Threading.Mutex($false, 'Local\WeChatHookWatchdog')
if (-not $mutex.WaitOne(0)) { exit 0 }
try {
    $wechatProc = Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'"
    $hookPort = Test-Port 30001
    $bridgeProc = Get-CimInstance Win32_Process -Filter "(Name='python.exe' or Name='pythonw.exe')" | Where-Object { $_.CommandLine -and $_.CommandLine -match 'bridge_hook\\main\.py' }
    $bridgePort = Test-Port 3002
    $astrPort = Test-Port 24874
    $logged = $false
    if ($hookPort) { try { $s = Invoke-RestMethod -Uri 'http://127.0.0.1:30001/QueryDB/status' -TimeoutSec 3; $logged = ([int]$s.IsLogin -eq 1) } catch { } }

    if ($hookPort) {
        try {
            $wp = (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq 30001 } | Select-Object -First 1).OwningProcess
            if ($wp) { Set-Content -Path (Join-Path $State 'wechat.pid') -Value $wp -Encoding ASCII }
        } catch { }
    }
$hookArgsOk = $false
try {
    $mains = Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' }
    foreach ($mw in $mains) { if ($mw.CommandLine -match 'StartPort=30001') { $hookArgsOk = $true } }
} catch { }
    if (-not $hookArgsOk) { $problems += "hookargs" }
    $problems = @()
    if (-not $wechatProc) { $problems += 'wechat' }
    if (-not $hookPort) { $problems += 'hook' }
    if (-not $bridgeProc -or -not $bridgePort) { $problems += 'bridge' }
    if (-not $astrPort) { $problems += 'astrbot' }

    $prev = $null
    if (Test-Path $StateFile) { try { $prev = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { } }
    $fails = 0; if ($prev -and $prev.fails) { $fails = [int]$prev.fails }
    if ($problems.Count) { $fails++ } else { $fails = 0 }

    $action = 'none'
    if ($problems.Count) {
        Write-Log ('problems: ' + ($problems -join ',') + " fails=$fails")
        if ($fails -ge 2) {
            $last = $null
            if (Test-Path $Stamp) { $last = (Get-Item $Stamp).LastWriteTime }
            $canRestartWeChat = (-not $last) -or (((Get-Date) - $last).TotalMinutes -ge 10)
            if (($problems -contains 'wechat') -or ($problems -contains 'hook')) {
                if ($canRestartWeChat) {
                    Write-Log 'restarting WeChat (hook missing)'
                    Stop-Process -Name Weixin -Force
                    Start-Sleep -Seconds 4
                    Start-Process -FilePath $WeChat -ArgumentList 'StartPort=30001 RecvType=2 CallBackURL="http://127.0.0.1:30002/event"'
                    Set-Content -Path $Stamp -Value (Get-Date -Format o) -Encoding UTF8
                    $action = 'restart-wechat'
                } else { $action = 'throttled' }
            } elseif (($problems -contains 'bridge')) {
                Write-Log 'restarting bridge'
                Start-Process -FilePath $Py -ArgumentList "`"$Bridge`"" -WorkingDirectory $Root -WindowStyle Hidden `
                    -RedirectStandardOutput (Join-Path $Logs 'hookbridge-stdout.log') `
                    -RedirectStandardError (Join-Path $Logs 'hookbridge-stderr.log')
                $action = 'restart-bridge'
            } elseif (($problems -contains 'astrbot')) {
                Write-Log 'AstrBot down; invoking start-hook-stack'
                & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $Root 'scripts\start-hook-stack.ps1')
                $action = 'start-stack'
            }
            $fails = 0
        }
    }

    [ordered]@{
        time = (Get-Date -Format o); problems = $problems; fails = $fails; action = $action
        wechat = [bool]$wechatProc; hook = $hookPort; logged_in = $logged
        bridge = $bridgePort; bridge_pid = (($bridgeProc | ForEach-Object { $_.ProcessId }) -join ','); astrbot = $astrPort
    } | ConvertTo-Json -Depth 4 | Set-Content -Path $StateFile -Encoding UTF8
} finally {
    $mutex.ReleaseMutex(); $mutex.Dispose()
}


