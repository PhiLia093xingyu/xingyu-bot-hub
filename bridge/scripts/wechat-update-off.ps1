# wechat-update-off.ps1
# 禁止 Weixin 4.x（微信 PC 版）自动更新。
# 适用场景：微信固定在 4.1.10.27，hook 栈（version.dll + OneBot）依赖该版本偏移，
#           一旦被自动升级到 4.1.15.x，hook 立即失效。
#
# 原理：
#   1. 杀掉更新执行器 WeixinUpdate.exe
#   2. 清空已下载/已暂存的更新包（download / patch）与更新状态 update.data
#   3. 对 update 树设置「拒绝 写入/删除/删除子项」ACE（可继承）
#      -> 客户端无法再写入更新状态、无法暂存补丁、无法删除/重建 patch 目录
#   4. 对 WeixinUpdate 插件目录设置「拒绝 执行」
#
# 效果：微信仍可正常收发消息，只是永远停在当前版本。
# 恢复：运行 wechat-update-on.ps1
#
# 幂等：可重复执行（不会叠加重复 ACE）。

$ErrorActionPreference = 'Stop'

$UpdateDir = Join-Path $env:APPDATA 'Tencent\xwechat\update'
$PluginDir = Join-Path $env:APPDATA 'Tencent\xwechat\xplugin\Plugins\WeixinUpdate'
$BackupDir = 'C:\ProgramData\WeChatBotStack\state\update-data-backup'
$User      = "$env:USERDOMAIN\$env:USERNAME"

$WriteRights  = [System.Security.AccessControl.FileSystemRights]'Write, Delete, DeleteSubdirectoriesAndFiles'
$ExecRights   = [System.Security.AccessControl.FileSystemRights]'ExecuteFile'

$Inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$NoProp  = [System.Security.AccessControl.PropagationFlags]::None

function Add-DenyRule {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][System.Security.AccessControl.FileSystemRights]$Rights
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Output "  skip (missing): $Path"
        return
    }
    $acl = Get-Acl -LiteralPath $Path
    $exists = $acl.Access | Where-Object {
        $_.AccessControlType -eq 'Deny' -and
        $_.IdentityReference.Value -eq $User -and
        $_.FileSystemRights -eq $Rights -and
        $_.InheritanceFlags -eq $Inherit
    }
    if ($exists) {
        Write-Output "  already blocked: $Path"
        return
    }
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($User, $Rights, $Inherit, $NoProp, 'Deny')
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
    Write-Output "  blocked: $Path"
}

Write-Output '=== 1) 停止更新执行器 ==='
$updaters = Get-Process -Name 'WeixinUpdate' -ErrorAction SilentlyContinue
if ($updaters) {
    $updaters | ForEach-Object { Write-Output ("  kill pid {0}" -f $_.Id) }
    $updaters | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
} else {
    Write-Output '  no WeixinUpdate process'
}

Write-Output '=== 2) 备份并清除已暂存更新 ==='
if (Test-Path -LiteralPath "$UpdateDir\update.data") {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    $bak = Join-Path $BackupDir ('update.data.{0}.bak' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
    Copy-Item -LiteralPath "$UpdateDir\update.data" -Destination $bak -Force
    Write-Output "  backed up -> $bak"
}
foreach ($sub in @('download', 'patch')) {
    $p = Join-Path $UpdateDir $sub
    if (Test-Path -LiteralPath $p) {
        $s = Get-ChildItem -LiteralPath $p -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum
        Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $p -Force | Out-Null
        $left = Get-ChildItem -LiteralPath $p -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum
        Write-Output ("  purged {0}: {1:N0} MB -> {2:N0} MB left" -f $sub, ($s.Sum / 1MB), ($left.Sum / 1MB))
    }
}
Remove-Item -LiteralPath "$UpdateDir\update.data" -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath "$UpdateDir\update.data") { Write-Output '  WARN: update.data still present' }
else { Write-Output '  update.data removed' }

Write-Output '=== 3) 锁死 update 树（拒绝写入/删除）==='
Add-DenyRule -Path $UpdateDir -Rights $WriteRights
Add-DenyRule -Path (Join-Path $UpdateDir 'patch')    -Rights $WriteRights
Add-DenyRule -Path (Join-Path $UpdateDir 'download') -Rights $WriteRights

Write-Output '=== 4) 禁用 WeixinUpdate 插件执行 ==='
Add-DenyRule -Path $PluginDir -Rights $ExecRights

Write-Output '=== 5) 验证 ==='
$blocked = 0; $failed = 0
# 5a) 不能再写入 update.data
try { Set-Content -LiteralPath "$UpdateDir\update.data" -Value 'probe' -ErrorAction Stop; $failed++ ; Write-Output '  TEST FAIL: update.data writable' }
catch { $blocked++; Write-Output '  OK: update.data write denied' }
# 5b) 不能在 patch 下建文件
try { New-Item -ItemType File -Path (Join-Path $UpdateDir 'patch\_probe') -ErrorAction Stop | Out-Null; $failed++; Write-Output '  TEST FAIL: patch writable' }
catch { $blocked++; Write-Output '  OK: patch create denied' }
# 5c) 不能删除 patch 目录
try { Remove-Item -LiteralPath (Join-Path $UpdateDir 'patch') -Force -Recurse -ErrorAction Stop; $failed++; Write-Output '  TEST FAIL: patch removable' }
catch { $blocked++; Write-Output '  OK: patch delete denied' }

Write-Output ('=== 结果: {0} denied, {1} failed ===' -f $blocked, $failed)
if ($failed -gt 0) { exit 1 }
exit 0
