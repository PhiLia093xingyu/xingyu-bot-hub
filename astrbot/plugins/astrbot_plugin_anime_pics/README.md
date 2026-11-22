# 二次元搜图（astrbot_plugin_anime_pics）

给 AstrBot 的 agent 补上"二次元图源"：pixiv 为主，yande.re / konachan 兜底，
抓到的图直接发进当前会话，不用再靠临时 python 脚本现搭。

## 为什么需要它

AstrBot 自带 `web_search` 的 `resource.image` 只接 pexels / pixabay，
里面几乎没有二次元插画，中文词还容易跑偏。本插件把 pixiv 的
`/ajax/search/artworks` 和 Moebooru 的 `post.json` 接进来，走本机代理，
所以群里有人要图时能直接出图。

## 装上之后

| 能力 | 说明 |
| --- | --- |
| LLM 工具 `anime_pic_search` | agent 自己判断要不要搜图、搜什么词；图由工具直接发出 |
| `/搜图 猫耳 橙发` | 手动搜，默认发 3 张 |
| `/搜图状态` | 看当前图源、代理、cookie 配置 |

工具参数：

- `keyword`：中/英/日文都行
- `source`：`auto`（默认，pixiv → yande）/ `pixiv` / `yande` / `konachan`
- `count`：要几张，0 用配置默认值
- `safe`：是否只要全年龄

## 配置（面板「插件配置」里改）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `proxy` | `http://127.0.0.1:7890` | 支持 `http://` 和 `socks5://`，iKuuu 的混合端口就是它；留空直连 |
| `pixiv_cookie` | 空 | 可选，填 `PHPSESSID` 的值；不填也能搜到一部分 |
| `enable_pixiv` / `enable_yande` / `enable_konachan` | 开 / 开 / 关 | 图源开关 |
| `default_count` / `max_count` | 3 / 8 | 张数 |
| `safe_mode` | true | 过滤 R18（pixiv 的 `xRestrict`、booru 的 `rating:safe`） |
| `search_order` | `popular_d` | pixiv 排序：`popular_d` 收藏热度 / `date_d` 最新 |
| `exclude_ai` | true | 过滤 AI 生成作品（pixiv `aiType == 2`） |
| `ja_tags` | true | 中文关键词自动补日文标签，原文和日文各搜一遍再合并 |
| `dedup_hours` | 6 | 这段时间内发过的作品不再重复发；0 = 关闭去重 |
| `timeout` | 20 | 单请求超时秒数 |
| `verify_ssl` | true | 代理做证书劫持导致连不上时可以关 |
| `cooldown` | 5 | 同一会话冷却秒数，防止模型自己刷图 |
| `cache_days` | 3 | 本地图片缓存保留天数 |

## 实现要点

- `sources.py`：纯 httpx 同步客户端，代理、重试（指数退避）、
  pixiv 缩略图 `250x250_80_a2/..._square1200.jpg` → `540x540_70/..._master1200.jpg` 的换档、
  booru 的 `tag.json` 词表校验（yande 没有 `orange_hair`、`maid` 这类标签，
  会被自动过滤并逐级放宽）。
- `main.py`：`@filter.llm_tool` 工具 + `/搜图` 命令，HTTP 全部丢进线程池，
  不阻塞 AstrBot 事件循环；发图用 `event.chain_result([Image.fromFileSystem(...)])`。
- 线路抖动是常态（pixiv 实测约 1/4 的请求会 SSL 断流），所以有
  单请求重试 + 同图源二次尝试 + 图源间兜底三层。

## 依赖

无额外依赖，只用 AstrBot 自带 venv 里的 `httpx`（0.28+，支持 socks5 需要 `socksio`，本机已装）。

## 状态（2026-11-08 21:55 实测）

- 已装进 `C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378\data\plugins\`，
  AstrBot 4.28.1 加载成功，日志：`Added llm tool: anime_pic_search`
- 面板「工具」里能看到 `anime_pic_search`，`active: true`，来源 `二次元搜图`
- 配置文件已生成：`data/config/astrbot_plugin_anime_pics_config.json`
- 直连测试（走 7890）：pixiv 搜索 6/6 成功、约 0.6 s 一次；
  `猫耳 橙发` 拿到 pixiv 1 张 + yande 1 张（跨源补齐）；冷却与下载都正常
- **没配 cookie** 也能搜到 pixiv 结果（只是结果集比登录态少），配了会更全

## 群内实测（2026-11-08 23:26，QQ 群 1121359826）

- 通过一次性 cron 任务唤醒主 agent，agent 自主调用 `anime_pic_search`
  （`keyword=猫耳, count=3, source=auto`）
- 23:26:41 发 2 张、23:26:50 发 1 张、23:26:52 补一句短评
- 期间一次 `i.pximg.net` 下载被代理抖掉（WARN），工具仍把成功的图发了出去
- 之后按群里的要求补了两项：`search_order=popular_d`（收藏热度）、
  `exclude_ai=true`；实测 `猫耳` 的 popular_d 结果 60 条里 28 条是 AI 作品，
  过滤后插件返回的 12 条与 AI 集合交集为空

## 中文关键词补日文标签（23:35）

`ZH_JA` 表 + `to_ja_tags()`：同一个词先用原文搜，再用日文标签搜一遍合并去重。

```
白发 少女    -> 白髪 少女
双马尾 水手服 -> ツインテール セーラー服
猫耳 睡衣    -> 猫耳 パジャマ
风景 壁纸    -> 風景 壁紙
```

## 去重（23:36）

`_sent` 记录 `source:id`，默认记忆 6 小时。整批都是旧图时会把搜索范围放宽到
`max(limit*4, limit+6)` 再搜一轮，仍然没有新的就返回
「这些图最近都发过了」，不再重复发。实测连调两次：第一次 2 张，第二次 2 张，交集为空。

### 自测

```powershell
& "C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378\.venv\Scripts\python.exe" `
  .\plugins\astrbot_plugin_anime_pics\selftest.py
```

直接调插件里的 `anime_pic_search` 协程（假 event），跑搜索 → 下载 → 组装图片链，
不需要经过 LLM，也不往真实会话发东西。

