# Hook 编译说明（WeChat 4.1.10.27）

`version.dll` 来自开源项目 `WechatHook4.1.10.27`（作者：mosheng20205），本包附带源码与预编译 DLL。

## 环境

- Windows 10/11 x64
- Visual Studio 2022 Build Tools（含"使用 C++ 的桌面开发"）

## 编译

```powershell
$msbuild = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
& $msbuild .\x64_Version_dll.vcxproj /p:Configuration=Release /p:Platform=x64 /m
```

产物：`x64\Release\version.dll`。

## 部署

```powershell
Copy-Item .\x64\Release\version.dll "C:\Program Files\Tencent\Weixin\version.dll" -Force
```

然后按 `docs/02-安装.md` 的启动参数启动微信。

## 说明

- 源码中的偏移/vtable 只对应 **4.1.10.27**，换版本必须重新逆向；
- 微信更新后 `version.dll` 可能被覆盖或加载失败，请关闭自动更新；
- 本包未包含任何账号/密钥信息，源码为上游开源代码（含少量作者注释）。
