# <BOT_NAME>吃饭啦 · astrbot_plugin_xingyu_dinner

对话里出现「<BOT_NAME>吃饭啦」，就现场画一张 DeepSeek 充值卡片发回会话。

## 用法

- 群里或私聊说一句 `<BOT_NAME>吃饭啦`（默认还有 `<BOT_NAME>吃饭了`、`投喂<BOT_NAME>`，忽略空格，「<BOT_NAME> 吃饭啦」也认）
- 想手动要一张：`/投喂卡`

命中的效果：先一句「唔，饭钱在这儿——¥12.34 的额度，扫完记得说一声。」，后面跟一张卡片。
卡片上有二维码（默认指向 `https://platform.deepseek.com/top_up`）、随机饭钱、当下时间、随机卡号。

## 它是真的在画

不是模板套字，也不是缓存图：每次触发都走一遍 Pillow 绘制，
纸纹噪点、卡号、饭钱、生成时间全是当下的，所以两张卡不会长得一模一样。
画卡放在 `asyncio.to_thread` 里跑，不堵事件循环；本地只保留最近 N 张，自动清理。

## 配置

WebUI - 插件管理 - <BOT_NAME>吃饭啦 - 配置：

| 键 | 说明 |
| --- | --- |
| `enable` | 总开关 |
| `triggers` | 触发暗号列表（忽略空格匹配） |
| `qr_url` | 二维码内容，换成自己的收款页也行 |
| `channel_label` | 卡片上显示的通道文字 |
| `card_title` / `card_subtitle` / `card_footer` | 卡片文案 |
| `amount_min` / `amount_max` | 随机饭钱区间（元） |
| `cooldown` | 同会话冷却秒数，冷却期内安静不刷屏 |
| `reply_text` | 卡片前那句话，`{amount}` 会被替换；留空只发图 |
| `stop_event` | 命中后是否阻止大模型继续顺着答（默认开） |
| `keep_files` | 本地保留卡片张数 |

## 依赖

`qrcode`、`pillow`（已写进 requirements.txt）。字体走系统 `msyhbd.ttc` / `msyh.ttc`，找不到会退回默认字体。

## 说明

- 只处理「收到的消息」，机器人自己说的话不会触发，省得自问自答刷屏。
- `<BOT_NAME>` 是唤醒词，AstrBot 会把它从 `message_str` 里剥掉，所以插件会同时比对原始消息文本——`<BOT_NAME>吃饭啦` 进来时看到的是 `吃饭啦`，两边的原文都要看一遍。
- 版本号停在 093 就好啦。
