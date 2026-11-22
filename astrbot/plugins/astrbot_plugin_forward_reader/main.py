"""合并转发消息读取器：把「聊天记录 / 合并转发」卡片展开成文本，让 LLM 真的能读到。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Forward, Reply
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_forward_reader"

SEGMENT_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "share": "[链接]",
    "music": "[音乐]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "forward": "[嵌套转发]",
    "node": "[转发节点]",
    "nodes": "[转发节点]",
    "markdown": "",
}


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class ForwardReaderPlugin(Star):
    """合并转发读取器：把「聊天记录」卡片里的内容展开给 LLM 看。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config: dict = config if isinstance(config, dict) else {}

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    # ---------------- OneBot 调用 ----------------
    @staticmethod
    def _raw_event(event: AstrMessageEvent) -> dict:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return raw if isinstance(raw, dict) else {}

    async def _call_action(self, event: AstrMessageEvent, action: str, **params):
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        raw = self._raw_event(event)
        if raw.get("self_id"):
            params["self_id"] = raw["self_id"]
        try:
            return await bot.call_action(action, **params)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[forward_reader] {action} 调用失败: {e}")
            return None

    async def _fetch_nodes(self, event: AstrMessageEvent, fid: Any) -> list | None:
        if not fid:
            return None
        data = await self._call_action(event, "get_forward_msg", id=str(fid))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("messages", "message", "data", "content"):
                nodes = data.get(key)
                if isinstance(nodes, list):
                    return nodes
                if isinstance(nodes, dict):
                    for sub in ("messages", "message"):
                        if isinstance(nodes.get(sub), list):
                            return nodes[sub]
        return None

    # ---------------- 渲染 ----------------
    def _render_segments(self, segs: Any) -> str:
        if isinstance(segs, str):
            return segs
        if isinstance(segs, dict):
            segs = [segs]
        if not isinstance(segs, list):
            return ""

        out: list[str] = []
        for seg in segs:
            if not isinstance(seg, dict):
                out.append(str(seg))
                continue
            stype = str(seg.get("type", ""))
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if stype == "text":
                out.append(str(data.get("text", "")))
            elif stype == "at":
                out.append(f"@{data.get('qq', '')}")
            elif stype == "file":
                name = data.get("file") or data.get("name") or ""
                out.append(f"[文件 {name}]" if name else "[文件]")
            elif stype in SEGMENT_PLACEHOLDER:
                out.append(SEGMENT_PLACEHOLDER[stype])
            else:
                out.append(f"[{stype}]")
        return "".join(out)

    @staticmethod
    def _node_sender(node: dict) -> str:
        sender = node.get("sender")
        if isinstance(sender, dict):
            name = (
                sender.get("card")
                or sender.get("nickname")
                or sender.get("nick")
                or sender.get("name")
                or sender.get("uin")
                or sender.get("user_id")
            )
            if name:
                return str(name)
        for key in ("name", "nickname", "sender_nickname", "uin", "user_id"):
            if node.get(key):
                return str(node[key])
        return "未知"

    def _render_forward(self, nodes: list, title: str = "") -> str:
        max_nodes = max(1, _as_int(self._cfg("max_nodes", 200), 200))
        head = f"【合并转发{('：' + title) if title else ''}】（共 {len(nodes)} 条）"
        lines: list[str] = [head]
        for idx, node in enumerate(nodes[:max_nodes], start=1):
            if isinstance(node, dict):
                sender = self._node_sender(node)
                content = node.get("content")
                if content is None:
                    content = node.get("message")
                text = self._render_segments(content).strip()
            else:
                sender, text = "未知", str(node)
            lines.append(f"{idx}. {sender}：{text or '(空)'}")
        if len(nodes) > max_nodes:
            lines.append(f"……还有 {len(nodes) - max_nodes} 条未展开")
        return "\n".join(lines)

    def _truncate(self, text: str) -> str:
        limit = _as_int(self._cfg("max_chars", 4000), 4000)
        if limit > 0 and len(text) > limit:
            return text[:limit] + f"\n……（内容过长，已截断，原长 {len(text)} 字）"
        return text

    # ---------------- 收集 ----------------
    async def _collect_forwards(
        self, event: AstrMessageEvent, force_reply: bool = False
    ) -> list[str]:
        """收集当前消息（含被引用的消息）里所有能展开的转发内容。"""
        blocks: list[str] = []
        message = getattr(getattr(event, "message_obj", None), "message", None) or []

        direct: list[Any] = []
        reply_targets: list[Any] = []
        for comp in message:
            if isinstance(comp, Forward):
                direct.append(getattr(comp, "id", None))
            elif isinstance(comp, Reply):
                cid = getattr(comp, "id", None)
                if force_reply:
                    reply_targets.append(cid)
                    continue
                quoted_text = (getattr(comp, "message_str", "") or "").strip()
                if quoted_text and quoted_text not in {
                    "[聊天记录]",
                    "[转发消息]",
                    "[合并转发]",
                }:
                    continue
                reply_targets.append(cid)

        for fid in direct:
            nodes = await self._fetch_nodes(event, fid)
            if nodes:
                blocks.append(self._render_forward(nodes))
            else:
                logger.info(f"[forward_reader] 转发 {fid} 未能取到内容")

        for rid in reply_targets:
            if not rid:
                continue
            try:
                msg = await self._call_action(event, "get_msg", message_id=int(rid))
            except (TypeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue
            segs = msg.get("message") or msg.get("content") or []
            if not isinstance(segs, list):
                continue
            for seg in segs:
                if isinstance(seg, dict) and seg.get("type") == "forward":
                    fid = (seg.get("data") or {}).get("id")
                    nodes = await self._fetch_nodes(event, fid)
                    if nodes:
                        blocks.append(self._render_forward(nodes))

        return blocks

    # ---------------- 早期注入：让空文本的转发消息也能进入 LLM ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def preload_forward(self, event: AstrMessageEvent):
        """在 LLM 请求前把转发内容并入 message_str。

        纯转发卡片的消息文本是空的，AstrBot 会当成空消息直接跳过；
        这里先把它展开，消息才有内容可读。
        """
        if not _as_bool(self._cfg("enable_auto_expand", True), True):
            return
        if event.get_extra("_forward_reader_injected"):
            return
        blocks = await self._collect_forwards(event)
        if not blocks:
            return

        body = "\n\n".join(self._truncate(b) for b in blocks)
        base = (event.message_str or "").strip()
        marker = "（以下是这张聊天记录卡片里的内容）"
        event.message_str = f"{base}\n{marker}\n{body}".strip() if base else f"{marker}\n{body}"
        event.set_extra("_forward_reader_injected", True)
        logger.info(f"[forward_reader] 已展开 {len(blocks)} 段转发内容")

    # ---------------- 手动指令 ----------------
    @filter.command("读转发", alias={"展开转发", "读聊天记录"})
    async def read_forward_cmd(self, event: AstrMessageEvent):
        """引用一条合并转发消息后发送本指令，确认内容已经读到。"""
        blocks = await self._collect_forwards(event, force_reply=True)
        if not blocks:
            yield event.plain_result(
                "唔……没找到能展开的聊天记录，要引用那条转发再试一次吗？"
            )
            return
        if _as_bool(self._cfg("show_full_in_chat", False), False):
            yield event.plain_result("\n\n".join(self._truncate(b) for b in blocks))
            return
        lines = sum(b.count("\n") + 1 for b in blocks)
        yield event.plain_result(
            f"读到啦，{len(blocks)} 张卡片、约 {lines} 行，我看过了。"
        )
