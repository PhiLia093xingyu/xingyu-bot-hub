# WeChatBotStack · 微信 Hook 机器人框架（作者：星屿）

把 **微信 4.1.10.27** 通过开源 hook（`version.dll`）接成 **OneBot v11**，对接 **AstrBot**，实现微信聊天机器人；另附可选的 QQ 侧（NapCat Desktop + SnowLuma）配置说明。

> 仅供学习/自用。仓库源码不包含微信、QQ、NapCat、SnowLuma 的二进制；微信整合包见 Release 附件，QQ 客户端请从官网下载。

## 功能

| 能力 | 状态 |
|---|---|
| 文本收发 | ✅ |
| 图片收发（收图自动定位 `.dat` 并解密） | ✅ |
| 收藏表情包识别（CDN 下载 → 首帧 PNG → 视觉模型） | ✅ |
| 群聊 @ 识别（转 OneBot `at` 段） | ✅ |
| 语音接收 → whisper 转文字 | ✅（内存扫 SILK → pilk → wav） |
| 语音发送 | ❌ 需 C++ 逆向（SILK 编码 + CDN 上传 + 语音消息构造） |
| 拍一拍 | ❌ 入口在微信内部系统消息链路，未定位 |
| 文件/视频发送 | ❌ hook 无接口 |
| 开机自启 / 看门狗 / 状态脚本 | ✅ |

## 架构

```
微信 4.1.10.27 + version.dll (hook)
   │ HTTP 127.0.0.1:30001（发送/联系人）
   │ POST 127.0.0.1:30002/event（收消息回调）
   ▼
bridge_hook（Python，OneBot v11）
   │ 反向 WS 127.0.0.1:24875/ws
   ▼
AstrBot 4.28.1（平台 wechat）
```

QQ 侧（可选）：`QQ NT ← NapCat Desktop（UI）← SnowLuma（OneBot 后端）→ AstrBot:24874`。

## 下载

| 文件 | 大小 | 说明 | 下载 |
|---|---|---|---|
| `WeChatBotStack_星屿_v1.zip` | 423.7 MB | 微信本体 + hook + 桥 + AstrBot + 文档（已脱敏） | [GitHub Release 下载](https://github.com/PhiLia093xingyu/WeChatBotStack/releases/download/v1.0.0/WeChatBotStack_v1.zip) |
| QQ 客户端 | — | QQ 官方安装包（腾讯官网） | [QQ 官方下载](https://im.qq.com/pcqq) |

> `QQBotStack_星屿_v2.zip`（397 MB，含 NapCat/SnowLuma 发行文件）因闭源组件许可原因不公开挂 Release，需要可联系作者。

SHA256：

```
WeChatBotStack_星屿_v1.zip  FCCDDB4EB331B802DDA8A418611DC26C7792CD1DBB19D6C553272C860C73622A
QQBotStack_星屿_v2.zip      00A0EA4E6CF005F435819CE1C13A425515F55970054E4ADC29C47D111A39FEAF
```

## 星屿的 QQ 入口

> NapCat 与 SnowLuma 原本是开源项目，后来因商业化/资本介入转向闭源并删库，源码已无法获取，所以现在只能打包它们的官方发行文件（二进制/JS bundle）。

```
QQ NT（官方客户端）
   ▲
NapCat Desktop（UI 外壳）
   ▲
SnowLuma（OneBot 后端，闭源发行文件）
   │ 反向 WS 127.0.0.1:24874/ws
   ▼
AstrBot（平台 qq / aiocqhttp）
```

- QQ 客户端：从 [QQ 官网](https://im.qq.com/pcqq) 安装；
- NapCat Desktop + SnowLuma：见 `optional-qq/` 的配置示例与说明；
- 本仓库不附带上述闭源二进制，QQ 侧离线包 `QQBotStack_星屿_v2.zip` 请联系作者获取。

## 快速开始

1. 读 [`docs/02-安装.md`](docs/02-安装.md)，按步骤安装微信 4.1.10.27 与 hook；
2. 安装依赖：`uv pip install -r bridge/requirements.txt`（Python 3.12）；
3. 安装 AstrBot 4.28.1：`pip install astrbot==4.28.1`；
4. 复制 `bridge/config/hook.example.json` 为 `hook.json` 并填写 wxid/Token；
5. AstrBot 新增 `aiocqhttp` 平台（反向 WS `24875`，Token 一致）；
6. 启动顺序：AstrBot → 桥 → 微信（带 `StartPort=30001 RecvType=2 CallBackURL=...`）。

详细配置、排障见 [`docs/`](docs/)：

- [01-架构](docs/01-架构.md)
- [02-安装](docs/02-安装.md)
- [03-配置](docs/03-配置.md)
- [04-使用与排障](docs/04-使用与排障.md)
- [05-安全与隐私](docs/05-安全与隐私.md)

## 目录

```
├─ bridge/            OneBot 桥（main.py、配置模板、脚本、依赖）
├─ hook/              开源 hook 源码（不含预编译 DLL）、编译说明、禁止自动更新脚本、微信多开源码
├─ astrbot/           插件源码 + 配置示例（AstrBot 本体请 pip 安装）
├─ docs/              架构/安装/配置/排障/安全文档
└─ optional-qq/       QQ 侧配置示例与说明（不含闭源二进制）
```

## 使用须知

- **只支持微信 4.1.10.27**：hook 偏移绑定该版本，升级会失效；包内附带禁止自动更新脚本。
- 微信是腾讯专有软件；NapCat/SnowLuma 为闭源项目。仓库仅包含自写/开源代码与文档，**不附带上述软件的二进制**；网盘包仅用于本机复原，请遵守各自许可，不要公开传播二进制。
- 仓库内所有账号、wxid、Token、API Key、聊天数据均已剔除，示例值为占位符。

## 致谢

- [WechatHook4.1.10.27](https://github.com/mosheng20205/WechatHook4.1.10.27)（微信 hook，开源）
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)（机器人框架）
- NapCat / SnowLuma（QQ 侧，闭源，版权归原作者）

## 许可

本仓库自写部分（桥、脚本、文档）采用 MIT；`hook/` 目录为上游开源代码，遵循其原许可；插件源码版权归各插件作者。
