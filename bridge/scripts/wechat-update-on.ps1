# wechat-update-on.ps1
# 恢复微信自动更新：移除 wechat-update-off.ps1 设置的拒绝 ACE。
# 注意：恢复后微信会在下次检查时重新下载并升级，hook 栈将失效（除非同步升级 hook 偏移）。

$ErrorActionPreference = 'Stop'

$UpdateDir = Join-Path $env:APPDATA 'Tencent\xwechat\update'
$PluginDir = Join-Path $env:APPDATA 'Tencent\xwechat\xplugin\Plugins\WeixinUpdate'
$User      = "$env:USERDOMAIN\$env:USERNAME"

function Remove-OurDenyRule {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { Write-Output "  skip (missing): $Path"; return }
    $acl = Get-Acl -LiteralPath $Path
    $ours = @($acl.Access | Where-Object {
        $_.AccessControlType -eq 'Deny' -and $_.IdentityReference.Value -eq $User
    })
    if (-not $ours) { Write-Output "  no deny rule: $Path"; return }
    $removed = 0
    foreach ($r in $ours) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $r.IdentityReference, $r.FileSystemRights, $r.InheritanceFlags, $r.PropagationFlags, 'Deny')
        $acl.RemoveAccessRule($rule) | Out-Null
        $removed++
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
    Write-Output ("  restored: {0} ({1} rule(s) removed)" -f $Path, $removed)
}

Write-Output '=== 移除拒绝规则 ==='
Remove-OurDenyRule -Path (Join-Path $UpdateDir 'patch')
Remove-OurDenyRule -Path (Join-Path $UpdateDir 'download')
Remove-OurDenyRule -Path $UpdateDir
Remove-OurDenyRule -Path $PluginDir

Write-Output '=== 验证（应可写入）==='
try {
    New-Item -ItemType File -Path (Join-Path $UpdateDir 'patch\_probe') -Force -ErrorAction Stop | Out-Null
    Remove-Item -LiteralPath (Join-Path $UpdateDir 'patch\_probe') -Force -ErrorAction SilentlyContinue
    Write-Output '  OK: patch writable again'
} catch {
    Write-Output ('  WARN: still blocked -> ' + $_.Exception.Message)
}
