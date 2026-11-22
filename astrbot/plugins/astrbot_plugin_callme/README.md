# 叫名字就应

群消息**正文**里只要出现名字（不必在开头，也不必 @），就唤醒并回复。

- 识别名字：<BOT_NAME> / philia / <BOT_ALIAS> / 093（大小写不敏感，093 两侧不能是数字）
- 只看 Plain 正文分片：At 段的昵称、引用消息里的内容都不算，避免群名片带「<BOT_NAME>」的人误触发
- 已被 @、引用 bot、或以唤醒词开头的消息交给 AstrBot 内置链路，本插件不重复处理
- 同一会话 8 秒冷却

## 踩过的坑

`event.should_call_llm(True)` 在 AstrBot 里表示**禁止**默认 LLM 请求（ProcessStage 判的是 `not event.call_llm`），
唤醒后必须传 `False`，否则会叫醒之后又把自己按回去。

v0.1.1 修复唤醒后被禁止调用 LLM；v0.1.2 改为只匹配正文分片。
