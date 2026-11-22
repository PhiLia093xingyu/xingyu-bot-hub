# AstrBot 4.28.1（随包源码与插件）

本目录用于复原机器人框架，**不包含** AstrBot 的 `data/`（对话、密钥、日志、人设、插件配置均已移除）。

## 目录

```
astrbot/
├─ astrbot_pkg/astrbot/        AstrBot 包源码（4.28.1 运行环境中的 site-packages 副本）
├─ plugins/                    插件源码（已清理运行态配置）
├─ cmd_config.example.json     配置字段参考（密钥已清空）
└─ README.md
```

## 安装

```powershell
uv venv --python 3.12 C:\AstrBot\.venv
uv pip install --python C:\AstrBot\.venv\Scripts\python.exe astrbot==4.28.1

# 首次运行，生成 data/ 与 WebUI
C:\AstrBot\.venv\Scripts\astrbot.exe run
```

WebUI：`http://127.0.0.1:6185`

## 配置

1. 服务提供商：填自己的 API Key（不要使用示例占位符）；
2. 平台：新增 `aiocqhttp`，ID 建议 `wechat`，反向 WS `ws://127.0.0.1:24875/ws`，Token 与桥一致；
3. 语音：`provider_stt_settings.enable=true`，选 whisper 类 provider；
4. 插件：把 `plugins/` 下需要的插件复制到 `<AstrBot>\data\plugins\`，重启后在 WebUI 配置。

## 插件说明

- 随包插件为源码，依赖需各自安装（见各插件 `requirements.txt`）；
- QQ 专属插件（空间/签到/戳一戳等）在微信平台不生效；
- 插件配置属于个人数据，本包不包含，需重新填写。
