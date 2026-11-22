from __future__ import annotations

import asyncio
import io
import json
import os
import random
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

DEDUP_TTL = 120

DEFAULT_CATALOG = (
    r"C:\ProgramData\NapCatQQ Desktop\components\SnowLuma\data\sys-face-catalog.json"
)

DEFAULT_PROMPT = (
    "我刚在群里发了一句「{msg}」，现在收到了这些表情回应：{desc}。"
    "请顺着这些表情的意思、结合上下文，自然地回应一句，20字以内，别解释你在做什么。"
)
DEFAULT_CTX = "[表情回应] 「{msg}」收到了 {desc}"


def unicode_face(emoji: str) -> str:
    """128557 这种就是 unicode 码位，直接还原成字符。"""
    try:
        code = int(emoji)
    except (TypeError, ValueError):
        return ""
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
        try:
            return chr(code)
        except ValueError:
            return ""
    return ""


class ReactionMemoPlugin(Star):
    """群聊表情回应：同类合并、写进上下文，并按需自然回应。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self._seen: dict[str, float] = {}
        self._last_reply: dict[str, float] = {}
        self._buf: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        self._faces: dict[str, str] = {}
        self._faces_mtime: float = -1.0

    # ---------- 小工具 ----------

    def _dup(self, key: str) -> bool:
        now = time.time()
        for k, ts in list(self._seen.items()):
            if now - ts > DEDUP_TTL:
                self._seen.pop(k, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def _faces_table(self) -> dict[str, str]:
        """表情 id -> 名字（/拜谢 这种），目录变了就重新读一次。"""
        path = str(self.cfg.get("face_catalog") or DEFAULT_CATALOG)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return self._faces
        if mtime == self._faces_mtime:
            return self._faces
        table: dict[str, str] = {}
        try:
            data = json.load(io.open(path, encoding="utf-8"))
            for pack in data.get("packs") or []:
                for item in pack.get("emojis") or []:
                    sid = str(item.get("qSid") or "").strip()
                    name = str(item.get("qDes") or "").strip().lstrip("/")
                    if sid and name:
                        table[sid] = name
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[reaction_memo] 读表情目录失败: {e}")
            return self._faces
        self._faces = table
        self._faces_mtime = mtime
        logger.info(f"[reaction_memo] 表情目录 {len(table)} 个")
        return self._faces

    def face_name(self, emoji: str) -> str:
        emoji = str(emoji)
        name = self._faces_table().get(emoji)
        if name:
            return name
        char = unicode_face(emoji)
        if char:
            return char
        return f"表情#{emoji}"

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return int(self.cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _float_cfg(self, key: str, default: float) -> float:
        try:
            return float(self.cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    async def _fetch_msg(self, event: AiocqhttpMessageEvent, raw: dict) -> dict | None:
        message_id = raw.get("message_id") or raw.get("message_seq")
        if not message_id:
            return None
        try:
            return await event.bot.get_msg(message_id=int(message_id))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[reaction_memo] get_msg failed: {e}")
            return None

    @staticmethod
    def _text_of(msg: dict | None, limit: int = 60) -> str:
        if not msg:
            return ""
        segs = msg.get("message")
        text = ""
        if isinstance(segs, list):
            parts = [
                str(s.get("data", {}).get("text", ""))
                for s in segs
                if isinstance(s, dict) and s.get("type") == "text"
            ]
            text = "".join(parts).strip()
            if not text:
                kinds = {s.get("type") for s in segs if isinstance(s, dict)}
                for kind, label in (
                    ("image", "[图片]"),
                    ("mface", "[表情]"),
                    ("face", "[表情]"),
                    ("record", "[语音]"),
                    ("video", "[视频]"),
                    ("file", "[文件]"),
                ):
                    if kind in kinds:
                        return label
        return text[:limit]

    # ---------- 上下文 ----------

    async def _append_history(self, umo: str, entries: list[dict]) -> None:
        if not entries:
            return
        cm = self.context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            logger.debug("[reaction_memo] 会话未建立，跳过写入")
            return
        conv = await cm.get_conversation(umo, cid)
        history = getattr(conv, "content", None) if conv else None
        if isinstance(history, str):
            try:
                history = json.loads(history or "[]")
            except Exception:  # noqa: BLE001
                history = []
        history = list(history or [])

        for entry in entries:
            last = history[-1] if history else None
            # 同上一条同为 user 且有文本时合并，避免打断对话结构
            if (
                last
                and last.get("role") == "user"
                and entry.get("role") == "user"
                and isinstance(last.get("content"), str)
                and isinstance(entry.get("content"), str)
            ):
                if entry["content"] not in last["content"]:
                    last["content"] = last["content"] + "\n" + entry["content"]
                continue
            history.append(entry)

        await cm.update_conversation(umo, cid, history=history)

    # ---------- 合并与回应 ----------

    def _schedule_flush(self, key: str) -> None:
        window = self._int_cfg("merge_window", 8)
        task = asyncio.create_task(self._flush(key, window))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _flush(self, key: str, window: int) -> None:
        await asyncio.sleep(max(0, window))
        buf = self._buf.pop(key, None)
        if not buf:
            return

        desc = self._describe(buf["emojis"])
        msg_text = buf.get("msg") or "（没读到内容）"
        umo = buf["umo"]
        ctx_text = str(self.cfg.get("template") or DEFAULT_CTX).format(
            msg=msg_text, desc=desc
        )

        gid = buf.get("gid", "")
        prob = self._float_cfg("reply_prob", 0.5)
        cooldown = self._int_cfg("cooldown", 60)
        now = time.time()
        can_reply = (
            prob > 0
            and random.random() < prob
            and now - self._last_reply.get(gid, 0.0) >= cooldown
        )

        entries = [{"role": "user", "content": ctx_text}]
        reply = ""
        if can_reply:
            prompt = str(self.cfg.get("prompt") or DEFAULT_PROMPT).format(
                msg=msg_text, desc=desc
            )
            reply = await self._ask_llm(umo, prompt)
            if reply:
                self._last_reply[gid] = now
                entries.append({"role": "assistant", "content": reply})

        await self._append_history(umo, entries)
        logger.info(f"[reaction_memo] 合并写入: {ctx_text}" + (f" -> 回应: {reply}" if reply else ""))

        if reply:
            try:
                await self.context.send_message(umo, MessageChain().message(reply))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[reaction_memo] 发送失败: {e}")

    def _describe(self, emojis: dict) -> str:
        parts = []
        for emoji, info in emojis.items():
            users = "、".join(sorted(info["users"]))
            count = f"×{info['count']}" if info["count"] > 1 else ""
            parts.append(f"{self.face_name(emoji)}{count}（{users}）")
        return "、".join(parts)

    async def _ask_llm(self, umo: str, prompt: str) -> str:
        try:
            provider = self.context.get_using_provider(umo=umo)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reaction_memo] 拿不到 provider: {e}")
            return ""
        if not provider:
            return ""
        contexts: list[dict] = []
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if cid:
                conv = await cm.get_conversation(umo, cid)
                history = getattr(conv, "content", None) or []
                if isinstance(history, str):
                    history = json.loads(history or "[]")
                for item in history[-8:]:
                    role = item.get("role")
                    content = item.get("content")
                    if role in ("user", "assistant") and isinstance(content, str):
                        contexts.append({"role": role, "content": content})
        except Exception:  # noqa: BLE001
            contexts = []
        try:
            resp = await provider.text_chat(prompt=prompt, contexts=contexts or None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reaction_memo] LLM 请求失败: {e}")
            return ""
        text = (getattr(resp, "completion_text", "") or "").strip()
        return text[:120]

    # ---------- 主逻辑 ----------

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_reaction(self, event: AiocqhttpMessageEvent):
        if not self.cfg.get("enable", True):
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice":
            return
        if raw.get("notice_type") != "group_msg_emoji_like":
            return

        is_add = raw.get("sub_type") == "add"
        if not is_add and not self.cfg.get("include_remove", False):
            return

        likes = raw.get("likes") or []
        emoji = str(likes[0].get("emoji_id", "")) if likes else ""
        uid = str(raw.get("operator_id") or raw.get("user_id") or "")
        self_id = str(event.get_self_id())
        if uid and uid == self_id:
            return

        message_id = str(raw.get("message_id") or raw.get("message_seq") or "")
        if self._dup(f"{raw.get('group_id')}:{message_id}:{uid}:{emoji}:{is_add}"):
            return

        msg = await self._fetch_msg(event, raw)
        sender = ((msg or {}).get("sender") or {})
        is_bot = str(sender.get("user_id", "")) == self_id
        if self.cfg.get("only_bot_message", True) and not is_bot:
            return

        key = f"{raw.get('group_id')}:{message_id}"
        buf = self._buf.get(key)
        if buf is None:
            sender_obj = event.message_obj.sender
            nickname = str(sender.get("card") or sender.get("nickname") or "")
            if not nickname:
                nickname = getattr(sender_obj, "nickname", None) or uid
            buf = {
                "gid": str(raw.get("group_id") or ""),
                "umo": event.unified_msg_origin,
                "msg": self._text_of(msg),
                "emojis": {},
            }
            self._buf[key] = buf
            self._schedule_flush(key)
        else:
            nickname = str(sender.get("card") or sender.get("nickname") or "") or uid

        info = buf["emojis"].setdefault(emoji, {"count": 0, "users": set()})
        if is_add:
            info["count"] += 1
        else:
            info["count"] = max(0, info["count"] - 1)
        info["users"].add(nickname)

    async def terminate(self):
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
