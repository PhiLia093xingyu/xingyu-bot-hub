"""叫名字就应：群消息正文里出现名字就唤醒并回复，不必在开头、也不必 @。

两个平台的消息形态不一样：
- QQ(aiocqhttp)：@ 是 At 分片，昵称不会进正文，所以只读 Plain 即可。
- 微信(aiocqhttp)：@ 是纯文本 `@昵称`，昵称会混进正文，必须先把 @ 片段摘出来单独判断，
  否则一个叫「陆毅的<BOT_NAME>」的人被人 @ 一下就会把 bot 叫起来。
"""

import re
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType

NAMES = ("<BOT_NAME>", "philia", "<BOT_ALIAS>")
CODE_RE = re.compile(r"(?<!\d)093(?!\d)")  # 093 两边不能再是数字，避免 0932 之类误伤

# 微信里 @ 自己也是纯文本，靠这几个特征认出「这是我自己」。
# 只认账号昵称 <BOT_NAME> / 微信号 <BOT_NAME>_xingyu（都含 <botname>）。
# 注意：·<BOT_NAME>倾月· 是别人，别加进来。换昵称记得同步，或用插件配置 self_names 追加。
SELF_HINTS = ("<botname>", "093_<BOT_NAME>")

# @昵称：微信的 @ 后面跟一个窄空格 \u2005 或普通空格
AT_RE = re.compile(r"@([^@\u2005\s]{1,40})")

COOLDOWN = 8  # 秒，同一会话内避免连续唤醒


def plain_text(event: AstrMessageEvent) -> str:
    """只取正文，跳过 At / 引用 / 图片等分片（QQ 有效）。"""
    parts: list[str] = []
    for seg in event.get_messages() or []:
        if isinstance(seg, Plain) or type(seg).__name__ == "Plain":
            parts.append(getattr(seg, "text", "") or "")
    return "".join(parts).strip()


class CallMePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._last: dict[str, float] = {}
        cfg = config if isinstance(config, dict) else {}
        extra = cfg.get("self_names") or []
        self.self_hints = tuple(SELF_HINTS) + tuple(str(x) for x in extra if str(x).strip())

    def _hit(self, text: str) -> bool:
        ats = AT_RE.findall(text)
        core = AT_RE.sub(" ", text)  # 去掉 @片段，剩下的才是真正「说出口」的字

        low = core.lower()
        if any(n in low for n in NAMES) or CODE_RE.search(core):
            return True

        # 正文里没名字：只认 @ 到我自己的那种
        for at in ats:
            at_low = at.lower()
            if any(h in at_low for h in self.self_hints):
                return True
        return False

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if event.get_sender_id() == event.get_self_id():
            return

        # 已被 @、引用 bot、或以唤醒词开头：交给内置链路，别重复处理
        if event.is_at_or_wake_command:
            return

        text = plain_text(event)
        if not text:
            return
        if not self._hit(text):
            return

        now = time.time()
        key = event.unified_msg_origin
        if now - self._last.get(key, 0.0) < COOLDOWN:
            return
        self._last[key] = now

        logger.info(f"[callme] 被叫到名字，唤醒会话 {key} | 正文：{text[:40]}")
        event.is_at_or_wake_command = True
        event.is_wake = True
        # 注意：should_call_llm(True) 表示「禁止」默认 LLM 请求，这里必须是 False
        event.should_call_llm(False)

    async def terminate(self):
        pass
