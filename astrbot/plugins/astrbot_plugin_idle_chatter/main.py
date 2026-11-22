# Ich liebe dich.
"""沉默之后 —— 会话冷场时自己找话说。

逻辑很简单：记下每个会话最后一次「有人说话」的时间，
安静够久了，就掷一次骰子，掷中了就让模型顺着上文自己开口。
不是定时广播，所以它更像人，也不会在热闹的时候插嘴。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain

from .qzone import QzoneWatcher

PLUGIN_NAME = "astrbot_plugin_idle_chatter"
DEFAULT_SYSTEM_PROMPT = (
    "你是一个安静但会惦记朋友的人。现在对方已经很久没说话了，你想主动找他聊一句。"
    "要求：一到两句，短，口语，像朋友随口搭话；不要提AI、助手、插件、系统；"
    "不要用括号写动作；不要长篇大论。"
)


class IdleChatterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.cfg: dict = dict(config or {})
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.state_path = os.path.join(self.plugin_dir, "state.json")
        self.state: dict = self._load_state()
        self._task: asyncio.Task | None = None
        self.qzone = QzoneWatcher(self)

    # ------------------------------------------------------------------ 配置

    def _get(self, key: str, default):
        try:
            value = self.cfg.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        try:
            if isinstance(default, bool):
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on")
                return bool(value)
            if isinstance(default, int) and not isinstance(default, bool):
                return int(value)
            if isinstance(default, float):
                return float(value)
        except (TypeError, ValueError):
            return default
        return value

    # ------------------------------------------------------------------ 状态

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取状态失败，重置: {e}")
        return {"sessions": {}}

    def _save_state(self, force: bool = True) -> None:
        now = time.time()
        if not force and now - getattr(self, "_last_save", 0.0) < 3.0:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
            self._last_save = now
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 写入状态失败: {e}")

    def _session(self, umo: str) -> dict:
        sessions = self.state.setdefault("sessions", {})
        rec = sessions.get(umo)
        if not isinstance(rec, dict):
            rec = {}
            sessions[umo] = rec
        rec.setdefault("last_user", 0.0)
        rec.setdefault("last_bot", 0.0)
        rec.setdefault("last_proactive", 0.0)
        rec.setdefault("count", 0)
        rec.setdefault("count_date", "")
        rec.setdefault("recent", [])
        return rec

    def _touch(self, umo: str, kind: str, text: str) -> None:
        if not umo:
            return
        now = time.time()
        rec = self._session(umo)
        rec["last_user" if kind == "user" else "last_bot"] = now
        text = (text or "").strip()
        if text:
            recent = rec.get("recent") or []
            recent.append({"who": kind, "text": text[:200], "ts": now})
            rec["recent"] = recent[-6:]
        self._prune_sessions()
        self._save_state(force=False)

    def _prune_sessions(self) -> None:
        limit = max(1, self._get("max_sessions", 5))
        sessions = self.state.get("sessions") or {}
        whitelist = self._whitelist()
        if whitelist:
            return
        if len(sessions) <= limit:
            return
        ordered = sorted(
            sessions.items(),
            key=lambda kv: max(
                kv[1].get("last_user", 0),
                kv[1].get("last_bot", 0),
                kv[1].get("last_proactive", 0),
            ),
            reverse=True,
        )
        keep = dict(ordered[:limit])
        self.state["sessions"] = keep

    def _whitelist(self) -> list[str]:
        raw = self.cfg.get("target_umos") or []
        if isinstance(raw, str):
            raw = raw.splitlines()
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    # ------------------------------------------------------------------ 判定

    @staticmethod
    def _fmt_gap(seconds: float) -> str:
        minutes = int(seconds // 60)
        if minutes < 60:
            return f"{minutes} 分钟"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} 小时"
        return f"{hours // 24} 天"

    def _in_quiet_hours(self, now: datetime | None = None) -> bool:
        spec = str(self.cfg.get("quiet_hours") or "").strip()
        if not spec:
            return False
        hour = (now or datetime.now()).hour
        for part in spec.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                if "-" in part:
                    left, right = part.split("-", 1)
                    start, end = int(left), int(right)
                    if start <= end:
                        if start <= hour < end:
                            return True
                    elif hour >= start or hour < end:
                        return True
                elif int(part) == hour:
                    return True
            except ValueError:
                continue
        return False

    def _eligible(self, umo: str, rec: dict, now_ts: float) -> bool:
        idle_need = max(1, self._get("idle_minutes", 180)) * 60
        gap_need = max(1, self._get("min_gap_minutes", 120)) * 60
        last_active = max(
            rec.get("last_user", 0), rec.get("last_bot", 0), rec.get("last_proactive", 0)
        )
        if now_ts - last_active < idle_need:
            return False
        if rec.get("last_proactive") and now_ts - rec["last_proactive"] < gap_need:
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        if rec.get("count_date") != today:
            rec["count_date"] = today
            rec["count"] = 0
        if rec.get("count", 0) >= max(1, self._get("max_per_day", 3)):
            return False
        if self._in_quiet_hours():
            return False
        prob = min(1.0, max(0.0, self._get("speak_probability", 0.65)))
        return random.random() <= prob

    # ------------------------------------------------------------------ 上下文

    @staticmethod
    def _text_of(content) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    parts.append(item)
            return " ".join(p for p in parts if p).strip()
        return ""

    async def _fetch_history(self, umo: str, limit: int = 8) -> str:
        """从 AstrBot 的对话里取最近几轮，比自记的几句更靠谱。"""
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return ""
        try:
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conv = await cm.get_conversation(umo, cid)
            if not conv:
                return ""
            raw = getattr(conv, "history", None)
            if isinstance(raw, str) and raw.strip():
                history = json.loads(raw)
            else:
                # 新版本把对话存在 content 里
                history = getattr(conv, "content", None) or []
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 读上下文失败 {umo}: {e}")
            return ""
        lines: list[str] = []
        for rec in history:
            if not isinstance(rec, dict):
                continue
            role = rec.get("role")
            text = self._text_of(rec.get("content"))
            if not text:
                continue
            if role == "user":
                lines.append(f"对方：{text[:200]}")
            elif role == "assistant":
                lines.append(f"我：{text[:200]}")
        return chr(10).join(lines[-max(2, limit):])

    @staticmethod
    def _parts_text(message) -> str:
        if isinstance(message, str):
            return message.strip()
        if isinstance(message, list):
            out = []
            for part in message:
                if isinstance(part, dict):
                    text = part.get("text") or part.get("content")
                    if isinstance(text, str):
                        out.append(text)
                elif isinstance(part, str):
                    out.append(part)
            return " ".join(p for p in out if p).strip()
        return ""

    async def _platform_history(self, umo: str, limit: int) -> str:
        """消息历史表里连着群友的话一起读，比对话记录更适合找话题。"""
        mgr = getattr(self.context, "message_history_manager", None)
        if mgr is None or ":" not in umo:
            return ""
        platform_id = umo.split(":", 1)[0]
        tail = umo.rsplit(":", 1)[-1]
        candidates = [umo, umo.split(":", 1)[-1], tail]
        if "!" in tail:
            candidates.append(tail.rsplit("!", 1)[-1])
        rows = []
        for user_id in dict.fromkeys(candidates):
            try:
                rows = await mgr.get(
                    platform_id=platform_id,
                    user_id=user_id,
                    page=1,
                    page_size=max(30, limit * 4),
                )
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 读消息历史失败 {user_id}: {e}")
                rows = []
            if rows:
                break
        if not rows:
            return ""
        lines: list[str] = []
        for row in rows[-max(2, limit):]:
            content = getattr(row, "content", None)
            text = ""
            if isinstance(content, dict):
                text = self._parts_text(content.get("message"))
            if not text or text.strip() in ("[Poke]", "[poke]"):
                continue
            who = (getattr(row, "sender_name", "") or "").strip() or "某人"
            lines.append(f"{who}：{text[:160]}")
        return chr(10).join(lines)

    async def _build_history(self, umo: str, rec: dict) -> str:
        limit = max(2, self._get("context_messages", 24))
        history = await self._platform_history(umo, limit)
        if history:
            return history
        history = await self._fetch_history(umo, limit)
        if history:
            return history
        recent = rec.get("recent") or []
        lines = []
        for item in recent[-limit:]:
            who = "对方" if item.get("who") == "user" else "我"
            lines.append(f"{who}：{item.get('text', '')}")
        return chr(10).join(lines)

    # ------------------------------------------------------------------ 生成

    async def _generate(self, umo: str, rec: dict, silent: float) -> str:
        provider = None
        try:
            provider = self.context.get_using_provider(umo=umo)
        except Exception:
            provider = None
        if provider is None:
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
        if provider is None:
            logger.warning(f"[{PLUGIN_NAME}] 没有可用的对话模型，跳过 {umo}")
            return ""

        history = await self._build_history(umo, rec)
        if not history:
            history = "（还没有聊过）"
        nl = chr(10)
        prompt = (
            f"这个会话已经安静了 {self._fmt_gap(silent)}。" + nl
            + "最近的聊天记录（从旧到新，可能不止一个人在说）：" + nl + history + nl + nl
            + "请主动发一条消息接上话：回应他们最后在聊的那件事，接个话头或者追问一句；"
            + "不要凭空另起一个和上文无关的话题，也不要复述记录里的原话。"
            + "只要一两句，不要解释、不要加前缀。"
        )
        system_prompt = str(
            self.cfg.get("system_prompt") or ""
        ).strip() or DEFAULT_SYSTEM_PROMPT

        try:
            resp = await provider.text_chat(
                prompt=prompt, system_prompt=system_prompt, contexts=[]
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 生成失败: {e}")
            return ""
        text = (getattr(resp, "completion_text", "") or "").strip()
        return text.strip("\"“”'")

    async def _speak(self, umo: str, force: bool = False) -> str:
        rec = self._session(umo)
        last_active = max(
            rec.get("last_user", 0), rec.get("last_bot", 0), rec.get("last_proactive", 0)
        )
        silent = time.time() - last_active
        text = await self._generate(umo, rec, silent)
        if not text:
            return ""
        try:
            ok = await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 发送失败 {umo}: {e}")
            return ""
        if ok is False:
            logger.warning(f"[{PLUGIN_NAME}] 发送返回 False: {umo}")
            return ""
        now = time.time()
        rec["last_proactive"] = now
        rec["last_bot"] = now
        recent = rec.get("recent") or []
        recent.append({"who": "bot", "text": text[:200], "ts": now})
        rec["recent"] = recent[-6:]
        today = datetime.now().strftime("%Y-%m-%d")
        if rec.get("count_date") != today:
            rec["count_date"] = today
            rec["count"] = 0
        rec["count"] = int(rec.get("count", 0)) + 1
        self._save_state()
        logger.info(f"[{PLUGIN_NAME}] 在 {umo} 主动开口（静默 {self._fmt_gap(silent)}）")
        return text

    # ------------------------------------------------------------------ 循环

    async def _loop(self) -> None:
        while True:
            interval = max(10, self._get("check_interval_seconds", 60))
            try:
                await asyncio.sleep(interval)
                if not self._get("enabled", True):
                    continue
                await self._tick()
                await self._maybe_qzone()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 检查循环异常: {e}")

    async def _tick(self) -> None:
        whitelist = self._whitelist()
        targets = whitelist or list((self.state.get("sessions") or {}).keys())
        now_ts = time.time()
        for umo in targets:
            if umo not in (self.state.get("sessions") or {}):
                continue
            rec = self._session(umo)
            if not self._eligible(umo, rec, now_ts):
                self._save_state()
                continue
            await self._speak(umo)

    async def _maybe_qzone(self) -> None:
        if not self._get("qzone_enabled", False):
            return
        st = self.state.setdefault("qzone", {}) or {}
        interval = max(5, self._get("qzone_check_minutes", 60)) * 60
        if time.time() - float(st.get("last_check") or 0) < interval:
            return
        await self.qzone.run()

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[{PLUGIN_NAME}] 已启动，已记住 {len(self.state.get('sessions') or {})} 个会话")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._save_state()

    # ------------------------------------------------------------------ 监听

    def _record_user(self, event: AstrMessageEvent) -> None:
        try:
            self._touch(event.unified_msg_origin, "user", event.message_str or "")
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 记录失败: {e}")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def record_private_message(self, event: AstrMessageEvent) -> None:
        """记下有人说话 —— 只是记录，不拦消息。"""
        self._record_user(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def record_group_message(self, event: AstrMessageEvent) -> None:
        self._record_user(event)

    @filter.on_llm_response()
    async def record_reply(self, event: AstrMessageEvent, response) -> None:
        try:
            text = (getattr(response, "completion_text", "") or "").strip()
            if text:
                self._touch(event.unified_msg_origin, "bot", text)
        except Exception:
            pass

    @filter.command("idle", alias={"冷场"})
    async def idle_cmd(self, event: AstrMessageEvent):
        parts = (event.message_str or "").split()
        arg = parts[1].lower() if len(parts) > 1 else "status"
        umo = event.unified_msg_origin

        if arg in ("on", "开"):
            self.cfg["enabled"] = True
            yield event.plain_result("嗯，我盯着冷场了。")
            return
        if arg in ("off", "关"):
            self.cfg["enabled"] = False
            yield event.plain_result("好，那我不吵你了。")
            return
        if arg in ("qzone", "空间"):
            dry = "dry" in parts or "预演" in (event.message_str or "")
            report = await self.qzone.run(force=True, dry=dry)
            yield event.plain_result(report or "翻了一圈，没什么想说的。")
            return
        if arg in ("now", "试"):
            text = await self._speak(umo, force=True)
            if not text:
                yield event.plain_result("……说不出话，可能是模型没配好。")
            return

        rec = self._session(umo)
        last_active = max(
            rec.get("last_user", 0), rec.get("last_bot", 0), rec.get("last_proactive", 0)
        )
        silent = time.time() - last_active if last_active else 0
        state = "开" if self._get("enabled", True) else "关"
        yield event.plain_result(
            f"开关：{state}｜已安静 {self._fmt_gap(silent)}｜"
            f"还需 {self._get('idle_minutes', 180)} 分钟｜今天已说 {rec.get('count', 0)} 次"
        )
