<#
.SYNOPSIS
    打开第二个微信实例（第二个账号），不影响正在运行的机器人实例。

.DESCRIPTION
    微信 4.1.10.27 的单实例限制有两个关键点（实测）：
      1. 只要给 Weixin.exe 传任何命令行参数（哪怕 Foo=bar），它就被当成
         「给已运行实例的指令」，1 秒内 exit -1。所以不能用参数给第二个
         实例指定 StartPort / CallBackURL。
      2. 客户端处于异常状态时（例如被强杀、或外部动过它的互斥体），
         无参启动也会被拦。此时重启一次主实例即可恢复。

    因此第二实例走「隔离目录 + 无参」：
      * C:\ProgramData\WeChatBotStack\wx2\Weixin.exe   —— exe 副本
      * 4.1.10.27 是指向真实安装目录的 Junction（不占空间）
      * 刻意不放 version.dll 和 wx.ini
        -> 不加载 hook，不会向 bridge(:30002) 回传事件，不会污染机器人账号

    注意：这个实例没有 hook，不能接入 AstrBot，只能人工使用。

.EXAMPLE
    .\weixin-second-account.ps1
#>
$ErrorActionPreference = 'Stop'

$src = 'C:\Program Files\Tencent\Weixin'
$dst = 'C:\ProgramData\WeChatBotStack\wx2'

# 1) 准备隔离目录
New-Item -ItemType Directory -Path $dst -Force | Out-Null
if (-not (Test-Path "$dst\Weixin.exe")) {
    Copy-Item "$src\Weixin.exe" "$dst\Weixin.exe" -Force
    Write-Output "copied Weixin.exe"
}
if (-not (Test-Path "$dst\4.1.10.27")) {
    New-Item -ItemType Junction -Path "$dst\4.1.10.27" -Target "$src\4.1.10.27" | Out-Null
    Write-Output "linked 4.1.10.27"
}
foreach ($bad in 'version.dll', 'wx.ini') {
    if (Test-Path "$dst\$bad") { Remove-Item "$dst\$bad" -Force; Write-Output "removed $bad (isolation)" }
}

# 2) 已经在跑就不重复开
$running = Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'" |
           Where-Object { $_.ExecutablePath -eq "$dst\Weixin.exe" }
if ($running) {
    Write-Output ("第二实例已在运行: pid " + (($running | ForEach-Object ProcessId) -join ', '))
    return
}

# 3) 无参启动
$p = Start-Process -FilePath "$dst\Weixin.exe" -WorkingDirectory $dst -PassThru
Write-Output "started pid=$($p.Id)"
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 1
    $p.Refresh()
    if ($p.HasExited) { break }
}
if ($p.HasExited) {
    Write-Output "❌ 启动失败 (exit=$($p.ExitCode))。通常是主实例状态异常，重启一次微信再试。"
    exit 1
}
Write-Output "✅ 第二实例已启动，请在它的窗口里扫码登录第二个账号。"
Write-Output "   （该实例无 hook，不接机器人；主实例的 hook 在 30001 不受影响）"
