# 可选：QQ 侧（NapCat Desktop + SnowLuma）

微信侧**不需要**本目录。只有你还想同时跑 QQ 机器人时才需要。

## 组件

| 组件 | 作用 | 获取 |
|---|---|---|
| QQ NT 客户端 | QQ 本体（体积数 GB，本包不包含） | 腾讯官网 |
| NapCat Desktop | UI 外壳（本包不包含安装包） | NapCat 官方发布 |
| SnowLuma | OneBot 后端（真正处理 QQ 消息） | **已打包**：`snowluma/runtime/`（闭源发行文件） |

## 已打包内容

- `snowluma/runtime/`：SnowLuma 当前发行文件（闭源，无源码可打包）；已剔除 `config/`、`data/`、`logs/` 等个人数据；
- `snowluma/onebot.example.json`：OneBot 配置模板（脱敏）。

## 配置模板

`onebot.example.json` 为 SnowLuma 的 OneBot 配置示例（已脱敏）：

- `account`：QQ 号；
- `onebot.ws_reverse.url`：AstrBot QQ 平台的反向 WS（通常 `ws://127.0.0.1:24874/ws`）；
- `onebot.ws_reverse.token`：与 AstrBot QQ 平台 Token 一致。

实际配置路径：`<NapCatQQ Desktop>\components\SnowLuma\config\onebot_<QQ号>.json`。

## 流程

1. 安装 QQ NT 与 NapCat Desktop；
2. 在 NapCat Desktop 中登录 QQ、启用 SnowLuma；
3. AstrBot 新增 `aiocqhttp` 平台（端口 24874），Token 与 SnowLuma 一致；
4. 启动顺序：AstrBot → NapCat/SnowLuma → QQ。

## 注意

- QQ 客户端与 SnowLuma 体积大，本包 1 GB 限制下未包含；
- QQ 专属插件（空间、签到、戳一戳等）只在 QQ 平台生效；
- 与微信侧互不影响，可同时运行。
