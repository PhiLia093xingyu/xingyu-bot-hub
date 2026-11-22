from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.context import Context

from .config import PluginConfig
from .utils import get_nickname


class LLMService:
    def __init__(self, context: Context, config: PluginConfig):
        self.context = context
        self.cfg = config

    async def get_conversation(self, event: AiocqhttpMessageEvent):
        """获取或创建当前会话的 conversation 对象"""
        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        try:
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                cid = await conv_mgr.new_conversation(umo, event.get_platform_id())
            return await conv_mgr.get_conversation(umo, cid)
        except Exception as e:
            logger.warning(f"[Pokepro] 获取 conversation 失败: {e}")
            return None

    async def recent_lines(
        self, event: AiocqhttpMessageEvent, limit: int | None = None
    ) -> list[str]:
        """把最近几条对话抠出来，塞进提示词里，免得回得牛头不对马嘴。"""
        if limit is None:
            try:
                limit = int(getattr(self.cfg.llm, "context_limit", 20))
            except (TypeError, ValueError):
                limit = 20
        if limit <= 0:
            return []
        conv = await self.get_conversation(event)
        history = getattr(conv, "content", None) if conv else None
        if isinstance(history, str):
            try:
                import json

                history = json.loads(history or "[]")
            except Exception:  # noqa: BLE001
                history = []
        lines: list[str] = []
        for item in (history or [])[-limit:]:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            who = "我" if item.get("role") == "assistant" else "对方"
            lines.append(f"{who}：{content.strip()[:80]}")
        return lines

    async def build_prompt(
        self, event: AiocqhttpMessageEvent, prompt_template: str
    ) -> str:
        """构建用户 prompt"""
        username = await get_nickname(
            event.bot, event.get_group_id(), event.get_sender_id()
        )
        prompt = prompt_template.format(username=username)
        lines = await self.recent_lines(event)
        if lines:
            prompt = (
                "以下是最近的对话，务必先读懂再回应，别答非所问：\n"
                + "\n".join(lines)
                + "\n\n"
                + prompt
            )
        return prompt
