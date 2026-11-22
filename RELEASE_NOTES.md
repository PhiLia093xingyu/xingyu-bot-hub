# Release v1.0.0（2026-11-22）

首个公开版本：微信 4.1.10.27 Hook 机器人框架 + 可选 QQ 侧配置。

## 内容

- `bridge/`：微信 → OneBot v11 桥（文本/图片/表情/语音接收、群 @、自启脚本）
- `hook/`：开源 hook 源码与编译说明、禁止微信自动更新脚本、微信多开源码
- `astrbot/`：插件源码与配置示例
- `docs/`：架构、安装、配置、排障、安全文档
- `optional-qq/`：QQ 侧（NapCat/SnowLuma）说明与配置示例

## 发行包（网盘，不在仓库内）

| 文件 | 大小 | 下载 |
|---|---|---|
| `WeChatBotStack_星屿_v1.zip` | 423.7 MB | [GitHub Release 附件](https://github.com/PhiLia093xingyu/xingyu-bot-hub/releases/download/v1.0.0/WeChatBotStack_v1.zip) |
| QQ 客户端 | — | [QQ 官网](https://im.qq.com/pcqq) |



SHA256：

```
WeChatBotStack_v1.zip       FCCDDB4EB331B802DDA8A418611DC26C7792CD1DBB19D6C553272C860C73622A
QQBotStack_v2.zip          18F013B311ACB68D73EE5C99DB9C592424204352D88155B789C278AD1AB1B5C7
```

## 已知限制

- 只支持微信 4.1.10.27；请执行 `hook/防止微信自动更新/4.x禁止微信自动升级.bat`。
- 语音发送、拍一拍、文件/视频发送未实现。
- QQ 侧需要自备 QQ NT 客户端；NapCat/SnowLuma 为闭源软件，仓库不附带二进制。
