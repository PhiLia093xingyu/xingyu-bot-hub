# bridge_hook（微信 → OneBot v11）

自写桥：把 WeChatHook 的回调转成 OneBot v11，反连 AstrBot 的 `wechat` 平台。

## 目录

```
bridge/
├─ bridge_hook/main.py       主程序
├─ config/hook.example.json  配置模板（复制为 hook.json）
├─ requirements.txt          Python 依赖
└─ scripts/                  启动/停止/巡检脚本（Windows）
```

## 运行

```powershell
# 依赖见 requirements.txt
uv pip install --python C:\ProgramData\WeChatBotStack\.venv\Scripts\python.exe -r requirements.txt

# 复制配置并填写
Copy-Item config\hook.example.json C:\ProgramData\WeChatBotStack\config\hook.json

# 启动（无窗口）
C:\ProgramData\WeChatBotStack\.venv\Scripts\pythonw.exe C:\ProgramData\WeChatBotStack\bridge_hook\main.py
```

## 配置要点

- `hook.self_wxid` / `hook.self_nickname`：机器人账号；
- `onebot.reverse.url/token`：必须与 AstrBot `wechat` 平台一致；
- `onebot.ws_server`：给其他 OneBot 客户端用（可不用）。

## 已实现能力

- 文本、图片收发；
- 群消息 `wxid_xxx:` 前缀剥离、`@机器人` 转 OneBot `at` 段；
- 收图片：定位微信 `.dat` → hook `/Decode_Pic` → 图片段；
- 收藏表情包：CDN 下载 → 首帧 PNG → 图片段；
- 收语音：Frida 扫内存 SILK → `pilk` 解码 → `record` 段；
- 事件过滤：`selfwxid` 与配置不一致的外部事件直接丢弃。

## 已知限制

- 语音/文件/视频**发送**未实现（hook 无接口）；
- 拍一拍未实现（入口未定位）；
- 语音接收依赖 Frida，每条约 1–2 秒额外耗时。
