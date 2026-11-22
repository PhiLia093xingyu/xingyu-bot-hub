# WeChat bot stack watchdog: run from Task Scheduler every 5 minutes.
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\WeChatBotStack'
$Logs = Join-Path $Root 'logs'
$State = Join-Path $Root 'state'
$Log = Join-Path $Logs 'watchdog.log'
$StateFile = Join-Path $State 'watchdog-state.json'
New-Item -ItemType Directory -Path $Logs, $State -Force | Out-Null

function Write-Log([string]$msg) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $Log -Value $line -Encoding UTF8
    if ((Get-Item $Log).Length -gt 2MB) {
        $tail = Get-Content $Log -Tail 1000
        Set-Content -Path $Log -Value $tail -Encoding UTF8
    }
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

# single instance
$mutex = New-Object System.Threading.Mutex($false, 'Local\WeChatStackWatchdog')
if (-not $mutex.WaitOne(0)) { exit 0 }
try {
    $wechat = [bool](Get-Process -Name Weixin -ErrorAction SilentlyContinue)
    $astrPort = (Test-Port 24874)
    $wxPort = (Test-Port 24875)
    $bridgePort = (Test-Port 3002)
    $bridgeProc = Get-ProcByCmd 'bridge\\main\.py'
    $status = $null
    $statusFile = Join-Path $State 'status.json'
    if (Test-Path $statusFile) {
        try { $status = Get-Content $statusFile -Raw | ConvertFrom-Json } catch { $status = $null }
    }
    $staleSeconds = 999999
    if ($status -and $status.ts) { $staleSeconds = [int]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds - [int]$status.ts }

    $problems = @()
    if (-not $wechat) { $problems += 'wechat' }
    if (-not $astrPort) { $problems += 'astrbot' }
    if (-not $bridgePort -or -not $bridgeProc) { $problems += 'bridge' }
    if ($bridgePort -and $bridgeProc -and $staleSeconds -gt 180) { $problems += "bridge-stale(${staleSeconds}s)" }

    $prev = $null
    if (Test-Path $StateFile) { try { $prev = Get-Content $StateFile -Raw | ConvertFrom-Json } catch { $prev = $null } }
    $fails = 0
    if ($prev -and $prev.fails) { $fails = [int]$prev.fails }
    if ($problems.Count -gt 0) { $fails = $fails + 1 } else { $fails = 0 }

    $action = 'none'
    if ($problems.Count -gt 0) {
        Write-Log ('problems: ' + ($problems -join ','))
        if ($fails -ge 2 -or -not $bridgeProc) {
            Write-Log 'invoking start-stack'
            & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $Root 'scripts\start-stack.ps1')
            $action = 'start-stack'
            $fails = 0
        } else {
            $action = 'wait'
        }
    }

    [ordered]@{
        time = (Get-Date -Format o)
        problems = $problems
        fails = $fails
        action = $action
        wechat = $wechat
        astrbot = $astrPort
        wechat_platform = $wxPort
        bridge = $bridgePort
        bridge_pid = (($bridgeProc | ForEach-Object { $_.ProcessId }) -join ',')
        status_age_seconds = $staleSeconds
    } | ConvertTo-Json -Depth 4 | Set-Content -Path $StateFile -Encoding UTF8
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

