"""群聊回复守卫：关键词精准回复、回复概率门、敏感词拦截、引用图。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from typing import Any, Iterable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.message_components import At, Face, Plain, Reply
from astrbot.api.message_components import Record as AstrRecord
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.keyword_store import KeywordStore
from .core.quote_render import (
    CardTheme,
    MessageSegment,
    QuoteMessage,
    ReplyMessage,
    render_quote_card,
)
from .core.quote_render import _parse_cq_string as parse_cq_string

PLUGIN_NAME = "astrbot_plugin_reply_guard"
MAX_QUOTE_MESSAGES = 30
MAX_REPLY_DEPTH = 3
KEYWORD_SPLIT_DELIMITER = "|||"


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        items: list[str] = []
        for chunk in value.replace("，", ",").replace("\n", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                items.append(chunk)
        return items
    return []


_QUOTE_COMMAND_RE = re.compile(r"^[/／]([qpt])(?:\s+(1?\d{1,2}))?$", re.IGNORECASE)
# 引用语音时转写会被拼到指令后面（"/t爱特里…"），所以也认“斜杠 + 指令”开头的写法。
# 斜杠是必须的：不然群里随便一个 "q" 都会被当成指令，把正常聊天吃掉。
_QUOTE_COMMAND_PREFIX_RE = re.compile(
    r"^[/／]([qpt])(?:\s+(1?\d{1,2}))?(?=$|[^A-Za-z])", re.IGNORECASE
)


def _pendant_url(data: dict) -> str:
    """不同适配器给的挂件字段名不一样，挨个试一遍。"""
    for key in ("avatar_pendant", "avatarPendant", "pendant", "avatar_frame"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for sub in ("url", "image", "img", "path", "source"):
                item = value.get(sub)
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return ""


def _parse_quote_command(text: str) -> tuple[int, str] | None:
    """认 /q、/p、/t、/q 3 这种写法。q=普通，p=带群头，t=群头+日期时间。

    引用语音时，管线会把转写塞进 message_str 前面，所以不能只盯着整串比。
    """
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        candidates.append(lines[-1])
    words = text.split()
    if words:
        candidates.append(words[-1])
    for candidate in candidates:
        match = _QUOTE_COMMAND_RE.match(candidate) or _QUOTE_COMMAND_PREFIX_RE.match(
            candidate
        )
        if match:
            count = max(
                1, min(_as_int(match.group(2) or 1, 1), MAX_QUOTE_MESSAGES)
            )
            return count, match.group(1).lower()
    return None


def _flatten(config: dict) -> dict[str, Any]:
    """面板的分组配置拍平成 key_subkey。"""
    flat: dict[str, Any] = {}
    for key, value in (config or {}).items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                flat[f"{key}_{sub_key}"] = sub_value
        else:
            flat[key] = value
    return flat


@register(
    PLUGIN_NAME,
    "coco",
    "群聊回复守卫：关键词精准回复 / 回复概率门 / 敏感词拦截 / 引用图",
    "0.2.4",
    "https://github.com/coco292931/astrbot_plugin_reply_guard",
)
class ReplyGuardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = _flatten(config if isinstance(config, dict) else {})
        self.config = cfg
        self._group_state: dict[str, bool] = {}
        self._member_count_cache: dict[str, tuple[int, float]] = {}

        try:
            self.data_root = get_astrbot_data_path()
        except Exception:
            self.data_root = "/AstrBot/data"
        self.data_dir = os.path.join(self.data_root, "plugin_data", PLUGIN_NAME)
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        self.cache_dir = os.path.join(self.data_dir, "cache")
        self.group_toggle_path = os.path.join(self.data_dir, "group_header.json")
        try:
            if os.path.isfile(self.group_toggle_path):
                with open(self.group_toggle_path, encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    self._group_state = {str(k): bool(v) for k, v in loaded.items()}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[reply_guard] 读群名开关状态失败: {e}")

        for path in (self.data_dir, self.avatar_dir, self.cache_dir):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as e:
                logger.warning(f"[reply_guard] 创建目录失败 {path}: {e}")

        # ---- 关键词回复 ----
        self.enable_keyword_reply = _as_bool(cfg.get("enable_keyword_reply"), True)
        xlsx_path = str(cfg.get("keyword_xlsx_path") or "").strip()
        if not xlsx_path:
            xlsx_path = os.path.join(self.data_dir, "keywords.xlsx")
        elif not os.path.isabs(xlsx_path):
            xlsx_path = os.path.join(self.data_root, xlsx_path)
        self.keyword_scope = _as_list(cfg.get("keyword_scope"))
        self.store = KeywordStore(
            xlsx_path,
            sheet=str(cfg.get("keyword_sheet") or ""),
            reload_interval=_as_int(cfg.get("keyword_reload_interval"), 60),
            skip_header=_as_bool(cfg.get("keyword_has_header"), False),
        )
        # B 列里用 ||| 分隔，拆成多条消息依次发送
        self.keyword_split_enabled = _as_bool(cfg.get("keyword_split_enabled"), True)
        self.keyword_split_delay = max(
            0.0, _as_float(cfg.get("keyword_split_delay"), 0.8)
        )

        # ---- 回复概率 ----
        self.enable_probability = _as_bool(cfg.get("enable_probability"), True)
        self.probability = min(
            1.0, max(0.0, _as_float(cfg.get("probability_value"), 1.0))
        )
        self.probability_mode = str(cfg.get("probability_mode") or "group_only")
        self.probability_scope = _as_list(cfg.get("probability_scope_list"))

        # ---- 敏感词拦截 ----
        self.enable_sensitive_filter = _as_bool(
            cfg.get("enable_sensitive_filter"), True
        )
        self.sensitive_words = _as_list(cfg.get("sensitive_words"))
        self.sensitive_scope = _as_list(cfg.get("sensitive_scope"))

        # ---- 引用图 ----
        self.enable_quote = _as_bool(cfg.get("enable_quote"), True)
        self.quote_font_path = str(cfg.get("quote_font_path") or "").strip()
        self.quote_proxy = str(cfg.get("quote_proxy") or "").strip()
        self.quote_scope = _as_list(cfg.get("quote_scope"))
        self.quote_show_badge = _as_bool(cfg.get("quote_show_badge"), True)
        self.quote_show_reaction = _as_bool(cfg.get("quote_show_reaction"), True)
        self.quote_show_honor = _as_bool(cfg.get("quote_show_honor"), True)
        self.quote_show_group = _as_bool(cfg.get("quote_show_group"), False)
        self.quote_stt = _as_bool(cfg.get("quote_stt"), True)
        self.quote_theme = CardTheme().with_overrides(
            card_bg=str(cfg.get("quote_card_bg") or "").strip(),
            bubble_bg=str(cfg.get("quote_bubble_bg") or "").strip(),
            emoji_style=str(cfg.get("quote_emoji_style") or "apple").strip(),
            reaction_style=str(cfg.get("quote_reaction_style") or "chip").strip(),
            tail=_as_bool(cfg.get("quote_bubble_tail"), True),
        )
        self._at_name_cache: dict[str, str] = {}
        self._member_cache: dict[str, dict] = {}
        self._member_meta_cache: dict[str, dict] = {}
        self._honor_cache: dict[str, dict] = {}
        self._member_raw: dict[str, dict] = {}
        self._member_records: dict[str, dict] = {}
        self._meta_error: str = ""
        self._reaction_debug: dict = {}
        self._getmsg_cache: dict[str, dict] = {}
        self._reply_debug: dict = {}
        self._honor_debug: dict = {}
        self._history_debug: dict = {}
        self._sender_sample: dict = {}
        self._last_window: list = []

    async def initialize(self) -> None:
        try:
            stamp = time.strftime(
                "%m-%d %H:%M:%S",
                time.localtime(os.path.getmtime(__file__)),
            )
        except OSError:
            stamp = "?"
        logger.info(f"[reply_guard] 已加载 main.py（文件时间 {stamp}）")
        logger.info(
            "[reply_guard] 已加载：气泡尖角+群头衔/等级+表情修复版 "
            f"(tail={self.quote_theme.tail}, badge={self.quote_show_badge})"
        )
        if self.enable_keyword_reply:
            self.store.load(force=True)
            if self.store.error:
                logger.warning(f"[reply_guard] 关键词表未就绪: {self.store.error}")

    # ------------------------------------------------------------------ 通用

    @staticmethod
    def _in_scope(event: AstrMessageEvent, scope: Iterable[str]) -> bool:
        scope_list = list(scope or [])
        if not scope_list:
            return True
        if "*" in scope_list:
            return True
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return any(item in (group_id, sender_id, umo) for item in scope_list)

    def _probability_applies(self, event: AstrMessageEvent) -> bool:
        is_group = bool(event.get_group_id())
        mode = self.probability_mode
        if mode == "group_only" and not is_group:
            return False
        if mode == "private_only" and is_group:
            return False
        return self._in_scope(event, self.probability_scope)

    def _find_sensitive(self, content: str) -> str:
        if not content or not self.sensitive_words:
            return ""
        lowered = content.lower()
        for word in self.sensitive_words:
            if word in content or word.lower() in lowered:
                return word
        return ""

    def _split_keyword_reply(self, reply: str) -> list[str]:
        """B 列里用 ||| 分隔的多段回复，拆成多条独立消息。"""
        text = str(reply or "")
        if not self.keyword_split_enabled or KEYWORD_SPLIT_DELIMITER not in text:
            return [text]
        parts = [part.strip() for part in text.split(KEYWORD_SPLIT_DELIMITER)]
        parts = [part for part in parts if part]
        return parts or [text]

    # ------------------------------------------------------------- 消息入口

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=120,
    )
    async def on_message(self, event: AstrMessageEvent):
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return

            text = (event.message_str or "").strip()

            # 0) 引用图：/q [数量]
            if self.enable_quote and self._in_scope(event, self.quote_scope):
                # 只看纯文本部分：@、引用、图片都不影响指令
                plain_text = "".join(
                    str(getattr(comp, "text", "") or "")
                    for comp in event.get_messages()
                    if isinstance(comp, Plain)
                ).strip()
                parsed = _parse_quote_command(plain_text) or _parse_quote_command(
                    text
                )
                if parsed is not None:
                    logger.info(
                        f"[reply_guard] 触发引用图 text={text!r} "
                        f"mode={parsed[1]} count={parsed[0]}"
                    )
                    quote_count, quote_mode = parsed
                    # should_call_llm(True) = 禁止默认 LLM 请求
                    event.should_call_llm(True)
                    async for result in self._quote_results(
                        event, quote_count, quote_mode
                    ):
                        yield result
                    return

            # 1) 关键词精准匹配：命中就按表格回，且不进历史
            if self.enable_keyword_reply and self._in_scope(
                event, self.keyword_scope
            ):
                self.store.reload_if_needed()
                reply = self.store.match(text)
                if reply:
                    parts = self._split_keyword_reply(reply)
                    logger.info(
                        f"[reply_guard] 关键词命中「{text}」 "
                        f"umo={event.unified_msg_origin} 分 {len(parts)} 条"
                    )
                    # should_call_llm(True) = 禁止默认 LLM 请求
                    event.should_call_llm(True)
                    for index, part in enumerate(parts):
                        if index and self.keyword_split_delay > 0:
                            await asyncio.sleep(self.keyword_split_delay)
                        yield event.plain_result(part)
                    return

            # 2) 概率门：没过就直接掐掉，消息也不落盘
            if not self.enable_probability:
                return
            if not self._probability_applies(event):
                return
            roll = random.random()
            if roll >= self.probability:
                # 概率没过：禁止默认 LLM 请求（True = 禁止）
                event.should_call_llm(True)
                logger.debug(
                    f"[reply_guard] 概率未通过 roll={roll:.4f} p={self.probability:.4f}"
                )
                return
        except Exception as e:
            logger.error(f"[reply_guard] 消息处理异常: {e}", exc_info=True)

    # --------------------------------------------------------- 敏感词拦截

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        if not self.enable_sensitive_filter or not self.sensitive_words:
            return
        if not self._in_scope(event, self.sensitive_scope):
            return

        hit = self._find_sensitive(self._collect_response_text(response))
        if not hit:
            return

        logger.info(
            f"[reply_guard] LLM 回复命中敏感词「{hit}」，已拦截 umo={event.unified_msg_origin}"
        )
        event.clear_result()
        event.stop_event()

    @staticmethod
    def _collect_response_text(response: LLMResponse | None) -> str:
        if response is None:
            return ""
        parts: list[str] = []
        text = getattr(response, "completion_text", "") or ""
        if text:
            parts.append(str(text))
        chain = getattr(response, "result_chain", None)
        for comp in getattr(chain, "chain", []) or []:
            if isinstance(comp, Plain):
                parts.append(str(getattr(comp, "text", "") or ""))
        return "\n".join(parts)

    # ----------------------------------------------------------- 引用图 /q

    async def _quote_results(
        self, event: AstrMessageEvent, count: int, mode: str = "q"
    ):
        reply_comp = None
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                reply_comp = comp
                break

        if reply_comp is None:
            yield event.plain_result("唔……得先引用一条消息，再发 /q 啦")
            return

        want = max(1, min(_as_int(count, 1), MAX_QUOTE_MESSAGES))
        messages: list[QuoteMessage] = []
        try:
            messages = await self._build_quote_messages(event, reply_comp, want)
        except Exception as e:
            logger.error(f"[reply_guard] 读取被引用消息失败: {e}", exc_info=True)

        if not messages:
            yield event.plain_result("那条消息我没读到，换一条试试？")
            return

        voice_count = sum(
            1 for m in messages for seg in m.segments if seg.type == "record"
        )
        logger.info(
            f"[reply_guard] 引用 {len(messages)} 条，语音段 {voice_count} 个，"
            f"stt={self.quote_stt}"
        )
        await self._transcribe_records(event, messages)
        await self._resolve_at_names(event, messages)
        if self.quote_show_badge:
            try:
                await self._apply_member_meta(event, messages)
            except Exception as e:
                logger.debug(f"[reply_guard] 补充群头衔/等级失败: {e}")
        self._dump_quote_debug(messages)

        show_group = mode in ("p", "t") or self.quote_show_group
        group_header = None
        if show_group:
            group_header = await self._group_header(event)
        timestamp = ""
        if mode == "t":
            stamp = next((m.time for m in messages if getattr(m, "time", 0)), 0)
            timestamp = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(stamp or time.time())
            )

        filename = f"quote_{int(time.time() * 1000)}"
        try:
            out_path = await render_quote_card(
                messages,
                out_dir=self.cache_dir,
                filename=filename,
                font_path=self.quote_font_path,
                proxy=self.quote_proxy,
                image_resolver=self._make_image_resolver(event),
                theme=self.quote_theme,
                group=group_header,
                timestamp=timestamp,
            )
        except Exception as e:
            logger.error(f"[reply_guard] 渲染引用图失败: {e}", exc_info=True)
            yield event.plain_result("图没画出来……稍等一下再试一次吧")
            return

        logger.info(f"[reply_guard] 引用图已生成: {out_path}（{len(messages)} 条消息）")
        yield event.chain_result([AstrImage.fromFileSystem(out_path)])

    def _show_group_now(self, event: AstrMessageEvent) -> bool:
        gid = str(event.get_group_id() or "")
        if gid in self._group_state:
            return self._group_state[gid]
        return self.quote_show_group

    def _set_group_toggle(self, event: AstrMessageEvent, value: bool) -> None:
        gid = str(event.get_group_id() or "")
        self._group_state[gid] = value
        try:
            with open(self.group_toggle_path, "w", encoding="utf-8") as fp:
                json.dump(self._group_state, fp, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reply_guard] 存群名开关状态失败: {e}")

    async def _audio_seconds(self, source: str) -> int:
        """拿音频文件量一下时长，取不到就 0。"""
        source = str(source or "").strip()
        if not source:
            return 0
        path = source
        temp_path = ""
        if source.startswith(("http://", "https://")):
            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        source, timeout=aiohttp.ClientTimeout(total=20)
                    ) as resp:
                        if resp.status != 200:
                            return 0
                        data = await resp.read()
                import tempfile

                fd, path = tempfile.mkstemp(suffix=".amr")
                os.close(fd)
                with open(path, "wb") as fp:
                    fp.write(data)
                temp_path = path
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[reply_guard] 下载语音失败: {e}")
                return 0
        try:
            import wave

            from astrbot.core.utils.media_utils import ensure_wav

            # QQ 语音是 silk，先转 wav（AstrBot 自带这套转换）
            wav_path = await ensure_wav(path)
            with wave.open(wav_path) as wav:
                frames = wav.getnframes()
                rate = wav.getframerate() or 1
            return max(1, int(round(frames / float(rate))))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[reply_guard] 量语音时长失败: {e}")
            return 0
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    async def _transcribe_records(self, event: AstrMessageEvent, messages) -> None:
        """把语音转成文字，塞进同一个气泡里。"""
        records = []

        def walk(msg, segs):
            for seg in segs:
                if seg.type == "record" and (seg.url or seg.id):
                    records.append(seg)
            if getattr(msg, "reply", None) is not None:
                walk(msg.reply, msg.reply.segments)

        for message in messages:
            walk(message, message.segments)
        if not records:
            return

        # 先补时长（消息段里常常没有秒数）
        for seg in records:
            if not str(seg.text or "").strip():
                seconds = await self._audio_seconds(seg.url)
                if seconds:
                    seg.text = str(seconds)
                    logger.info(f"[reply_guard] 语音时长: {seconds}s")

        if not self.quote_stt:
            return

        try:
            provider = await self.context.get_using_stt_provider_async(
                event.unified_msg_origin
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[reply_guard] 拿不到语音识别 provider: {e}")
            provider = None
        if provider is None:
            logger.info("[reply_guard] 没配置语音转文字，语音只画波形")
            return

        for seg in records:
            if seg.transcript or not seg.url:
                continue
            try:
                text = await asyncio.wait_for(
                    provider.get_text(audio_url=seg.url), timeout=30
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[reply_guard] 语音转写失败: {e}")
                continue
            text = str(text or "").strip()
            if text:
                seg.transcript = text
                logger.info(f"[reply_guard] 语音转写: {text[:40]}")

    async def _group_header(self, event: AstrMessageEvent) -> dict | None:
        """取群名和群头像，画在卡片顶上。"""
        try:
            gid = event.get_group_id()
        except Exception:  # noqa: BLE001
            gid = ""
        if not gid:
            return None
        name = ""
        group = getattr(event.message_obj, "group", None)
        name = str(getattr(group, "group_name", "") or "").strip()
        if not name:
            try:
                info = await event.bot.get_group_info(group_id=int(gid))
                name = str(info.get("group_name") or "").strip()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[reply_guard] 取群信息失败 {gid}: {e}")
        count = await self._group_member_count(event, gid)
        display = name or f"群 {gid}"
        if count:
            display = f"{display}（{count}）"
        avatar = f"https://p.qlogo.cn/gh/{gid}/{gid}/100"
        return {"name": display, "avatar": avatar}

    async def _group_member_count(self, event: AstrMessageEvent, gid: str) -> int:
        """群成员数，按群缓存 5 分钟，别每次都去翻名单。"""
        cached = self._member_count_cache.get(gid)
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        group = getattr(event.message_obj, "group", None)
        count = _as_int(getattr(group, "member_count", 0), 0) if group else 0
        if not count:
            try:
                info = await event.bot.get_group_info(group_id=int(gid))
                for key in ("member_count", "member_num", "memberCount"):
                    value = info.get(key)
                    if value:
                        count = _as_int(value, 0)
                        break
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[reply_guard] 取群人数失败 {gid}: {e}")
        if not count:
            try:
                members = await event.bot.get_group_member_list(group_id=int(gid))
                count = len(members or [])
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[reply_guard] 取群成员列表失败 {gid}: {e}")
        if count:
            self._member_count_cache[gid] = (count, time.time())
        return count

    # ------------------------------------------------------- 消息收集与解析

    def _dump_quote_debug(self, messages: list[QuoteMessage]) -> None:
        """把这次拼进图里的内容落一份，出问题了直接看。"""
        try:
            payload = [
                {
                    "nickname": message.nickname,
                    "user_id": message.user_id,
                    "segments": [
                        f"{segment.type}:{segment.kind}:{segment.text or segment.url or segment.id}"
                        for segment in message.segments
                    ],
                    "reply": message.reply.nickname if message.reply else None,
                    "message_id": message.message_id,
                    "time": message.time,
                    "reactions": len(message.reactions or []),
                    "title": message.title,
                    "level": message.level,
                    "pendant": bool(message.pendant),
                }
                for message in messages
            ]
            window = []
            for item in getattr(self, "_last_window", []) or []:
                sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
                window.append(
                    {
                        "message_id": item.get("message_id"),
                        "message_seq": item.get("message_seq"),
                        "time": item.get("time"),
                        "user_id": sender.get("user_id"),
                        "raw": str(item.get("raw_message") or "")[:60],
                    }
                )
            path = os.path.join(self.cache_dir, "last_quote.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "rendered": payload,
                        "window": window,
                        "sender_sample": getattr(self, "_sender_sample", {}),
                        "meta_error": getattr(self, "_meta_error", ""),
                        "reaction_debug": getattr(self, "_reaction_debug", {}),
                        "reply_debug": getattr(self, "_reply_debug", {}),
                        "honor_debug": getattr(self, "_honor_debug", {}),
                        "history_debug": getattr(self, "_history_debug", {}),
                        "meta_raw": dict(list(getattr(self, "_member_raw", {}).items())[:3]),
                    },
                    handle,
                    ensure_ascii=False,
                    indent=1,
                )
        except Exception as e:
            logger.debug(f"[reply_guard] 写调试信息失败: {e}")

    async def _group_member_names(self, client, group_id: str) -> dict[str, str]:
        """拉一次群成员表，10 分钟内复用。"""
        now = time.time()
        cached = self._member_cache.get(group_id)
        if cached and now - float(cached.get("at") or 0) < 600:
            self._member_records = dict(cached.get("records") or {})
            return dict(cached.get("names") or {})

        try:
            result = await client.call_action(
                "get_group_member_list", group_id=int(group_id)
            )
        except Exception as e:
            logger.debug(f"[reply_guard] 取群成员列表失败: {e}")
            return {}

        raw: list = []
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                raw = data
            elif isinstance(data, dict) and isinstance(data.get("list"), list):
                raw = data["list"]
        elif isinstance(result, list):
            raw = result

        names: dict[str, str] = {}
        records: dict[str, dict] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            qq = str(item.get("user_id") or "")
            if qq:
                names[qq] = str(item.get("card") or item.get("nickname") or "")
                records[qq] = item
        self._member_records = records
        self._member_cache[group_id] = {
            "at": now,
            "names": names,
            "records": records,
        }
        logger.info(f"[reply_guard] 群成员表已缓存 {len(names)} 人")
        return names

    @staticmethod
    def _pick_likes(raw: Any) -> list[dict]:
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict) and item.get("likes_cnt")]

    async def _fetch_reactions(
        self,
        event: AstrMessageEvent,
        message_id: str,
        target_time: int = 0,
        target_user: str = "",
    ) -> list[dict]:
        """表情回应：先问 get_msg，再翻历史窗口。

        NapCat 的 get_msg 对较早的消息常常返回空的 emojiLikesList，
        所以还得去 get_group_msg_history 里那几条上找一遍。
        """
        self._reaction_debug = {"target": str(message_id)}
        client = self._get_client(event)

        # 1) SnowLuma 那条专线：一条消息的全部表情回应
        if client is not None:
            try:
                mid: Any = int(str(message_id))
            except (TypeError, ValueError):
                mid = message_id
            try:
                result = await client.call_action(
                    "get_msg_emoji_likes", message_id=mid
                )
            except Exception as e:
                logger.debug(f"[reply_guard] get_msg_emoji_likes 失败: {e}")
                result = None
            data = result.get("data") if isinstance(result, dict) else result
            if isinstance(data, dict):
                data = data.get("data") or data
            self._reaction_debug["summary_type"] = type(data).__name__
            items: list[dict] = []
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    emoji_id = item.get("emoji_id")
                    count = item.get("count")
                    if emoji_id in (None, "") or not count:
                        continue
                    items.append({"emoji_id": str(emoji_id), "likes_cnt": count})
            self._reaction_debug["from_summary"] = len(items)
            if items:
                logger.info(f"[reply_guard] 表情回应 {len(items)} 条（get_msg_emoji_likes）")
                return items

        # 2) 老路：get_msg 里带的 emoji_likes_list
        info = await self._fetch_onebot_message(event, message_id)
        self._reaction_debug["getmsg_keys"] = getattr(self, "_last_getmsg_keys", None)
        likes = self._pick_likes((info or {}).get("emoji_likes_list"))
        self._reaction_debug["from_getmsg"] = len(likes)
        if likes:
            logger.info(f"[reply_guard] 表情回应 {len(likes)} 条（get_msg）")
            return likes

        group_id = str(event.get_group_id() or "")
        if not group_id or client is None:
            return []

        for source, kwargs in (
            ("get_group_msg_history", dict(group_id=int(group_id), count=60)),
            ("get_group_msg_history", dict(group_id=int(group_id), message_seq=0, count=60)),
        ):
            try:
                result = await client.call_action(source, **kwargs)
            except Exception as e:
                logger.debug(f"[reply_guard] {source} 取回应失败: {e}")
                continue

            data = result.get("data") if isinstance(result, dict) else result
            raw: list = []
            if isinstance(data, dict):
                for key in ("messages", "message", "list"):
                    if isinstance(data.get(key), list):
                        raw = data[key]
                        break
            elif isinstance(data, list):
                raw = data
            if not raw:
                self._reaction_debug["history"] = "empty"
                continue
            self._reaction_debug["history_size"] = len(raw)
            first = next((x for x in raw if isinstance(x, dict)), {})
            self._reaction_debug["history_keys"] = sorted(first.keys())
            with_likes = [
                x for x in raw
                if isinstance(x, dict) and x.get("emoji_likes_list")
            ]
            self._reaction_debug["history_with_likes"] = len(with_likes)
            self._reaction_debug["history_ids"] = [
                str(x.get("message_id")) for x in raw[:6] if isinstance(x, dict)
            ]

            def _same(item: dict) -> bool:
                if str(item.get("message_id") or "") == str(message_id):
                    return True
                # AstrBot 给的 id 和历史里的不一定是同一个，退一步按时间和发送人对
                if not target_time:
                    return False
                item_time = _as_int(item.get("time"), 0)
                if abs(item_time - target_time) > 3:
                    return False
                sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
                return not target_user or str(sender.get("user_id") or "") == str(target_user)

            matched = [item for item in raw if isinstance(item, dict) and _same(item)]
            self._reaction_debug["history_matched"] = len(matched)
            for item in matched:
                likes = self._pick_likes(item.get("emoji_likes_list"))
                if likes:
                    logger.info(f"[reply_guard] 表情回应 {len(likes)} 条（历史窗口）")
                    return likes
            break

        logger.info(
            "[reply_guard] 表情回应为空，get_msg 字段："
            f"{getattr(self, '_last_getmsg_keys', None)}"
        )
        return []

    async def _honor_via_web(
        self, event: AstrMessageEvent, group_id: str
    ) -> dict[str, str]:
        """SnowLuma 那条路空的时候，自己拿 cookie 去抓荣誉页。

        荣誉页的 category：1 龙王、2 群聊之火、3 群聊炽焰。
        """
        client = self._get_client(event)
        if client is None:
            return {}
        try:
            cookie_result = await client.call_action(
                "get_cookies", domain="qun.qq.com"
            )
        except Exception as e:
            self._honor_debug["web_error"] = f"get_cookies: {str(e)[:120]}"
            return {}
        data = cookie_result.get("data") if isinstance(cookie_result, dict) else cookie_result
        cookie = str((data or {}).get("cookies") or "") if isinstance(data, dict) else str(data or "")
        if not cookie:
            self._honor_debug["web_error"] = "没有 qun.qq.com 的 cookie"
            return {}
        self._honor_debug["web_cookie_len"] = len(cookie)

        try:
            import aiohttp
        except ImportError:
            self._honor_debug["web_error"] = "没有 aiohttp"
            return {}

        labels = {"1": "龙王", "2": "之火", "3": "炽焰"}
        honors: dict[str, str] = {}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Cookie": cookie,
            "Referer": "https://qun.qq.com/",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                for category, label in labels.items():
                    url = (
                        "https://qun.qq.com/interactive/honorlist?"
                        f"gc={group_id}&type={category}"
                    )
                    async with session.get(url) as resp:
                        if resp.status // 100 != 2:
                            self._honor_debug.setdefault("web_status", {})[category] = resp.status
                            continue
                        html = await resp.text()
                    match = re.search(r"window\.__INITIAL_STATE__=(.*?);", html)
                    if not match:
                        self._honor_debug.setdefault("web_status", {})[category] = "no-state"
                        continue
                    try:
                        state = json.loads(match.group(1).strip())
                    except Exception:
                        self._honor_debug.setdefault("web_status", {})[category] = "bad-json"
                        continue
                    items = state.get("talkativeList") or state.get("actorList") or []
                    self._honor_debug.setdefault("web_items", {})[category] = len(items)
                    for item in items:
                        qq = str(item.get("uin") or "")
                        if qq and qq not in honors:
                            honors[qq] = label
        except Exception as e:
            self._honor_debug["web_error"] = str(e)[:150]
        return honors

    async def _group_honors(
        self, event: AstrMessageEvent, group_id: str
    ) -> dict[str, str]:
        """群互动标识：龙王 / 群聊之火 / 群聊炽焰，10 分钟缓存。"""
        if not group_id:
            return {}
        cached = self._honor_cache.get(group_id)
        if cached and time.time() - float(cached.get("_at") or 0) < 600:
            return dict(cached.get("honors") or {})

        client = self._get_client(event)
        if client is None:
            return {}
        self._honor_debug = {"group": group_id}
        merged: dict[str, Any] = {}
        for htype in ("all", "talkative", "performer", "legend"):
            try:
                result = await client.call_action(
                    "get_group_honor_info", group_id=int(group_id), type=htype
                )
            except Exception as e:
                self._honor_debug.setdefault("errors", []).append(f"{htype}: {str(e)[:120]}")
                continue

            self._honor_debug.setdefault(
                "shapes", []
            ).append(f"{htype}:{type(result).__name__}")
            data: Any = result
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict) and isinstance(data.get("data"), dict):
                    data = data["data"]
            if isinstance(data, dict):
                for key, value in data.items():
                    if value:
                        merged.setdefault(key, value)
            if merged:
                break

        data = merged
        if not isinstance(data, dict) or not data:
            self._honor_debug["empty"] = True
            logger.info(f"[reply_guard] 群荣誉接口为空，改抓网页: {self._honor_debug}")
            honors_web = await self._honor_via_web(event, group_id)
            self._honor_debug["web_picked"] = len(honors_web)
            logger.info(f"[reply_guard] 网页兜底结果: {self._honor_debug}")
            self._honor_cache[group_id] = {
                "_at": time.time(),
                "honors": honors_web,
            }
            return honors_web

        self._honor_debug["data_keys"] = sorted(data.keys())
        self._honor_debug["sample"] = str(
            {k: (v[:1] if isinstance(v, list) else v) for k, v in data.items() if v}
        )[:300]
        honors: dict[str, str] = {}
        for key, label in (
            ("current_talkative", "龙王"),
            ("performer_list", "之火"),
            ("legend_list", "炽焰"),
        ):
            value = data.get(key)
            items = [value] if isinstance(value, dict) else value
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                qq = str(item.get("user_id") or "")
                if qq and qq not in honors:
                    honors[qq] = label
        self._honor_debug["picked"] = len(honors)
        self._honor_cache[group_id] = {"_at": time.time(), "honors": honors}
        logger.info(f"[reply_guard] 群荣誉已缓存 {len(honors)} 人")
        return honors

    async def _member_meta(
        self, event: AstrMessageEvent, user_id: str
    ) -> dict[str, str]:
        """群头衔 / 群等级 / 头像挂件，10 分钟内复用。"""
        group_id = str(event.get_group_id() or "")
        if not group_id or not str(user_id or "").isdigit():
            return {}
        key = f"{group_id}:{user_id}"
        cached = self._member_meta_cache.get(key)
        if cached and time.time() - float(cached.get("_at") or 0) < 600:
            return cached

        client = self._get_client(event)
        if client is None:
            self._meta_error = "拿不到 OneBot 客户端"
            return {}

        data: dict | None = None
        # 先看群成员表里有没有现成的（那份本来就要拉来查 @ 名字）
        record = (getattr(self, "_member_records", {}) or {}).get(str(user_id))
        if not record:
            try:
                await self._group_member_names(client, group_id)
                record = (getattr(self, "_member_records", {}) or {}).get(str(user_id))
            except Exception as e:
                logger.debug(f"[reply_guard] 拉群成员表失败: {e}")
        if isinstance(record, dict) and ("title" in record or "level" in record):
            data = record

        if data is None:
            try:
                result = await client.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                )
            except Exception as e:
                self._meta_error = f"get_group_member_info 失败: {e}"
                logger.debug(f"[reply_guard] 取群成员资料失败 {user_id}: {e}")
                return {}
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, dict):
                self._meta_error = f"返回结构不认识: {str(result)[:120]}"
                return {}
        self._member_raw[str(user_id)] = data
        meta = {
            "title": str(data.get("title") or "").strip(),
            "level": str(data.get("level") or "").strip(),
            "pendant": _pendant_url(data),
            "_at": time.time(),
        }
        self._member_meta_cache[key] = meta
        logger.debug(
            f"[reply_guard] 成员资料 {user_id}: 头衔={meta['title']!r} "
            f"等级={meta['level']!r} 挂件={'有' if meta['pendant'] else '无'}"
        )
        return meta

    async def _apply_member_meta(
        self, event: AstrMessageEvent, messages: "list[QuoteMessage]"
    ) -> None:
        for message in messages:
            meta = await self._member_meta(event, message.user_id)
            if not meta:
                continue
            message.title = meta.get("title") or message.title
            message.level = meta.get("level") or message.level
            message.pendant = meta.get("pendant") or message.pendant
        if self.quote_show_honor:
            honors = await self._group_honors(event, str(event.get_group_id() or ""))
            for item in messages:
                item.honor = honors.get(str(item.user_id), "")

        got = sum(1 for item in messages if item.title or item.level)
        logger.info(
            f"[reply_guard] 头衔/等级取到 {got}/{len(messages)} 条"
            + (f"，原因：{self._meta_error}" if got == 0 and self._meta_error else "")
        )

    async def _resolve_at_names(
        self, event: AstrMessageEvent, messages: list[QuoteMessage]
    ) -> None:
        """把 @ 的号换成群名片/昵称，顺手把 at 段变成文本段。"""
        group_id = str(event.get_group_id() or "")
        wanted: dict[str, str] = {}

        def collect(segments: list[MessageSegment]) -> None:
            for segment in segments:
                if segment.type == "at" and segment.id:
                    wanted.setdefault(segment.id, segment.text or "")

        def walk_collect(reply: ReplyMessage | None) -> None:
            if reply is None:
                return
            collect(reply.segments)
            walk_collect(reply.reply)

        for message in messages:
            collect(message.segments)
            walk_collect(message.reply)

        missing = [
            qq
            for qq, name in wanted.items()
            if not name and qq != "all" and qq.isdigit()
        ]
        if missing and group_id:
            client = self._get_client(event)
            if client is not None:
                for qq in missing:
                    cached = self._at_name_cache.get(f"{group_id}:{qq}")
                    if cached:
                        wanted[qq] = cached

                # 先用一次群成员列表把名字对上，比逐个查省事
                if [qq for qq in missing if not wanted.get(qq)]:
                    for qq, name in (
                        await self._group_member_names(client, group_id)
                    ).items():
                        if qq in missing and name and not wanted.get(qq):
                            wanted[qq] = name
                            self._at_name_cache[f"{group_id}:{qq}"] = name

                for qq in [q for q in missing if not wanted.get(q)]:
                    try:
                        info = await client.call_action(
                            "get_group_member_info",
                            group_id=int(group_id),
                            user_id=int(qq),
                        )
                    except Exception as e:
                        logger.debug(f"[reply_guard] 取群名片失败 {qq}: {e}")
                        continue
                    data = info.get("data") if isinstance(info, dict) else None
                    if isinstance(data, dict):
                        name = str(data.get("card") or data.get("nickname") or "")
                        if name:
                            wanted[qq] = name
                            self._at_name_cache[f"{group_id}:{qq}"] = name

            logger.info(
                f"[reply_guard] @ 名称解析 {len([q for q in wanted if wanted[q]])}/{len(wanted)}"
            )

        def apply(segments: list[MessageSegment]) -> None:
            for segment in segments:
                if segment.type != "at":
                    continue
                if segment.id == "all":
                    segment.type = "text"
                    segment.text = "@全体成员"
                    continue
                name = wanted.get(segment.id) or segment.text or segment.id
                segment.type = "text"
                segment.text = f"@{name}"

        def walk_apply(reply: ReplyMessage | None) -> None:
            if reply is None:
                return
            apply(reply.segments)
            walk_apply(reply.reply)

        for message in messages:
            apply(message.segments)
            walk_apply(message.reply)

    async def _build_quote_messages(
        self, event: AstrMessageEvent, reply_comp, count: int
    ) -> list[QuoteMessage]:
        first = await self._build_quote_message(event, reply_comp)
        if first is None:
            return []
        if count <= 1:
            return [first]

        before = await self._fetch_previous_messages(event, reply_comp, count, first)
        return (before + [first])[-count:]

    async def _fetch_onebot_reply(
        self, event: AstrMessageEvent, message_id: str | None, depth: int
    ) -> ReplyMessage | None:
        """递归取被引用的消息，最多三层，取不到就写占位。"""
        if not message_id or depth > MAX_REPLY_DEPTH:
            return None
        data = await self._fetch_onebot_message(event, message_id)
        if not data:
            return None
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        nickname = str(sender.get("card") or sender.get("nickname") or "")
        segments, reply_id = self._parse_onebot_message(data)
        if not segments:
            segments = [MessageSegment(type="text", text="[引用消息不可用]")]
        return ReplyMessage(
            nickname=nickname or "匿名",
            segments=segments,
            reply=await self._fetch_onebot_reply(event, reply_id, depth + 1),
        )

    async def _enrich_reply(
        self, event: AstrMessageEvent, reply: ReplyMessage | None, depth: int
    ) -> ReplyMessage | None:
        """引用链里只有 id 没内容的节点，用 get_msg 补上。"""
        if reply is None or depth > MAX_REPLY_DEPTH:
            return reply
        empty = not reply.segments or (
            len(reply.segments) == 1
            and reply.segments[0].type == "text"
            and reply.segments[0].text in ("[引用消息不可用]", "")
        )
        if empty and reply.message_id:
            fetched = await self._fetch_onebot_reply(event, reply.message_id, depth)
            if fetched is not None:
                if not fetched.nickname or fetched.nickname == "匿名":
                    fetched.nickname = reply.nickname or fetched.nickname
                reply = fetched
        reply.reply = await self._enrich_reply(event, reply.reply, depth + 1)
        return reply

    async def _build_quote_message(
        self, event: AstrMessageEvent, reply_comp
    ) -> QuoteMessage | None:
        nickname = str(getattr(reply_comp, "sender_nickname", "") or "")
        user_id = str(getattr(reply_comp, "sender_id", "") or "")
        segments: list[MessageSegment] = []
        reply: ReplyMessage | None = None
        message_id = str(getattr(reply_comp, "id", "") or "")
        msg_time = _as_int(getattr(reply_comp, "time", 0), 0)

        chain = list(getattr(reply_comp, "chain", None) or [])
        if chain:
            segments, reply = self._parse_astr_chain(chain)

        # 管线会把引用里的语音直接换成转写文字，语音条就画不出来了。
        # 这种情况回到原始消息里把 record 段捞回来，转写留着复用。
        chain_text = "".join(
            seg.text for seg in segments if seg.type == "text"
        ).strip()
        if message_id and chain:
            data = await self._fetch_onebot_message(event, message_id)
            if data:
                # Reply.time 往往是「发指令」的时间，时间戳一律以原消息为准
                data_time = _as_int(data.get("time"), 0)
                if data_time:
                    msg_time = data_time
                if not any(seg.type == "record" for seg in segments):
                    raw_segments, _raw_reply = self._parse_onebot_message(data)
                    records = [seg for seg in raw_segments if seg.type == "record"]
                    if records:
                        if chain_text:
                            for seg in records:
                                seg.transcript = chain_text
                        segments = raw_segments

        if not segments and not reply:
            data = await self._fetch_onebot_message(event, message_id or None)
            if data:
                sender = (
                    data.get("sender") if isinstance(data.get("sender"), dict) else {}
                )
                nickname = str(
                    sender.get("card") or sender.get("nickname") or nickname
                )
                user_id = str(sender.get("user_id") or user_id)
                message_id = str(data.get("message_id") or message_id)
                # Reply.time 往往是当前那条命令的时间，优先用消息自己的时间
                msg_time = _as_int(data.get("time"), msg_time)
                segments, reply_id = self._parse_onebot_message(data)
                reply = await self._fetch_onebot_reply(event, reply_id, 1)
        else:
            reply = await self._enrich_reply(event, reply, 1)

        if not segments and not nickname:
            return None

        # AstrBot 给的 Reply 往往只有 id，不带嵌套引用；这种情况自己去问那条消息
        if reply is None and message_id:
            info = await self._fetch_onebot_message(event, message_id)
            self._reply_debug = {"target": str(message_id), "got": bool(info)}
            if isinstance(info, dict):
                msg = info.get("message")
                if isinstance(msg, list):
                    self._reply_debug["segment_types"] = [
                        str(seg.get("type")) for seg in msg if isinstance(seg, dict)
                    ]
                else:
                    self._reply_debug["message_type"] = type(msg).__name__
                self._reply_debug["raw"] = str(info.get("raw_message") or "")[:120]
                _, nested_id = self._parse_onebot_message(info)
                self._reply_debug["nested_id"] = nested_id
                if nested_id:
                    reply = await self._fetch_onebot_reply(event, nested_id, 1)

        reactions: list[dict] = []
        if self.quote_show_reaction and message_id:
            try:
                reactions = await self._fetch_reactions(
                    event, message_id, msg_time, user_id
                )
            except Exception as e:
                logger.debug(f"[reply_guard] 取表情回应失败: {e}")

        return QuoteMessage(
            user_id=user_id,
            nickname=nickname,
            segments=segments,
            reply=reply,
            message_id=message_id,
            time=msg_time,
            reactions=reactions,
        )

    async def _fetch_previous_messages(
        self, event: AstrMessageEvent, reply_comp, count: int, quoted: QuoteMessage
    ) -> list[QuoteMessage]:
        """取被引用消息之前的若干条，拼在它前面（被引用那条排最后）。"""
        want = max(0, count - 1)
        self._history_debug = {"want": want, "count": count}
        if want <= 0:
            self._history_debug["bail"] = "count<=1"
            return []
        group_id = str(event.get_group_id() or "")
        # 私聊没有群号，得走 get_friend_msg_history
        if not group_id:
            peer = str(quoted.user_id or "")
            self_id = str(event.get_self_id() or "")
            # 她引用的可能是自己（机器人）发的消息，那对方其实是当前发消息的人
            if not peer.isdigit() or (self_id and peer == self_id):
                peer = str(event.get_sender_id() or "")
            if not peer.isdigit():
                self._history_debug["bail"] = "私聊里找不到对话对象"
                return []
            self._history_debug["peer"] = peer
        client = self._get_client(event)
        if client is None:
            self._history_debug["bail"] = "没有 OneBot 客户端"
            return []
        target_id = quoted.message_id or str(getattr(reply_comp, "id", "") or "")
        if not target_id:
            self._history_debug["bail"] = "没有目标消息 id"
            return []

        # 多拉一点，靠 message_id 在窗口里定位被引用那条，再取它前面的
        fetch_count = min(max(int(count) * 10, 120), 200)
        self._history_debug["fetch_count"] = fetch_count
        if group_id:
            action = "get_group_msg_history"
            payload = lambda size: {"group_id": int(group_id), "count": size}  # noqa: E731
        else:
            action = "get_friend_msg_history"
            anchor_raw = str(quoted.message_id or "")
            try:
                anchor = int(anchor_raw)
            except (TypeError, ValueError):
                anchor = 0
            self._history_debug["anchor"] = anchor
            # SnowLuma 的好友历史只读本地存储，给个锚点（被引用那条）才翻得动
            payload = lambda size: {  # noqa: E731
                "user_id": int(self._history_debug["peer"]),
                "count": size,
                "message_id": anchor,
                "reverse_order": True,
            }

        result = None
        last_error = ""
        for size in (fetch_count, 100, 50, 20):
            try:
                result = await client.call_action(action, **payload(size))
                self._history_debug["fetch_ok"] = f"{action}:{size}"
                break
            except Exception as e:
                last_error = f"{size}: {str(e)[:120]}"
                continue
        if result is None:
            self._history_debug["bail"] = f"get_group_msg_history 全失败: {last_error}"
            logger.warning(f"[reply_guard] get_group_msg_history 失败: {last_error}")
            return []

        raw = []
        if isinstance(result, dict):
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            if isinstance(data, dict):
                for key in ("messages", "message", "list"):
                    value = data.get(key)
                    if isinstance(value, list):
                        raw = value
                        break
        if not isinstance(raw, list):
            self._history_debug["bail"] = f"返回结构不认识: {type(result).__name__}"
            return []
        self._history_debug["raw"] = len(raw)

        records = [item for item in raw if isinstance(item, dict)]
        if not records:
            self._history_debug["bail"] = "窗口为空"
            return []

        def _key(item: dict) -> tuple[int, int]:
            seq = _as_int(item.get("message_seq"), 0) or _as_int(
                item.get("message_id"), 0
            )
            return (_as_int(item.get("time"), 0), seq)

        ordered = sorted(records, key=_key)
        keys = [_key(item) for item in ordered]
        if keys and keys[0] == keys[-1]:
            # 时间和序号都拿不到，按接口惯例（新在前）反过来
            ordered = list(reversed(records))

        target_id = str(getattr(reply_comp, "id", "") or "")
        quoted_ts = _as_int(getattr(reply_comp, "time", 0), 0)
        self._last_window = ordered[-30:]
        logger.info(
            "[reply_guard] 历史窗口: "
            + ", ".join(
                f"{item.get('message_id')}@{_as_int(item.get('time'), 0)}"
                for item in ordered[:12]
            )
            + f" | 目标 {target_id}@{quoted_ts} | 需要前 {want} 条"
        )

        self._history_debug = {
            "want": want,
            "target_id": target_id,
            "window": len(ordered),
            "quoted_time": quoted.time,
        }
        candidates: list[dict] = []
        start = -1

        # 先精确对 message_id；对不上再按「时间最近 + 同一个人」兜底。
        # 不能一遍扫完：同一人连着发的两条时间只差 1 秒，先扫到的会是错的那条。
        for index, item in enumerate(ordered):
            if str(item.get("message_id") or "") == target_id:
                start = index
                break

        if start < 0 and quoted.time:
            best: tuple[int, int] | None = None
            for index, item in enumerate(ordered):
                delta = abs(_as_int(item.get("time"), 0) - quoted.time)
                if delta > 2:
                    continue
                sender = (
                    item.get("sender") if isinstance(item.get("sender"), dict) else {}
                )
                if quoted.user_id and str(sender.get("user_id") or "") != str(
                    quoted.user_id
                ):
                    continue
                if best is None or delta <= best[0]:
                    best = (delta, index)
            if best is not None:
                start = best[1]
                self._history_debug["matched_by"] = "time"
        if start >= 0:
            candidates = ordered[max(0, start - want) : start]
        elif quoted.time:
            candidates = [
                item
                for item in ordered
                if _as_int(item.get("time"), 0)
                and _as_int(item.get("time"), 0) < quoted.time
            ][-want:]
        else:
            return []

        self._history_debug["start"] = start
        self._history_debug["candidates"] = len(candidates)
        try:
            lo = max(0, start - 4)
            hi = min(len(ordered), start + 3)
            self._history_debug["around"] = [
                (
                    index,
                    _as_int(ordered[index].get("time"), 0),
                    str(ordered[index].get("message_id")),
                    str(ordered[index].get("raw_message") or "")[:14],
                )
                for index in range(lo, hi)
            ]
            self._history_debug["dup_target"] = sum(
                1
                for item in ordered
                if str(item.get("message_id") or "") == target_id
            )
        except Exception:
            pass
        previous: list[QuoteMessage] = []
        for item in candidates:
            message = await self._onebot_to_quote(event, item)
            if message is not None:
                previous.append(message)
        return previous[-want:]

    async def _onebot_to_quote(
        self, event: AstrMessageEvent, data: dict
    ) -> QuoteMessage | None:
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        if sender and not self._sender_sample:
            self._sender_sample = sender
        nickname = str(sender.get("card") or sender.get("nickname") or "")
        user_id = str(sender.get("user_id") or "")
        segments, reply_id = self._parse_onebot_message(data)
        if not segments and not nickname:
            return None
        # 历史窗口里那几条也可能是“引用别人”的，别把它的引用扔掉
        nested: ReplyMessage | None = None
        if reply_id:
            try:
                nested = await self._fetch_onebot_reply(event, reply_id, 1)
            except Exception as e:
                logger.debug(f"[reply_guard] 取历史消息的引用失败: {e}")
        return QuoteMessage(
            user_id=user_id,
            nickname=nickname,
            segments=segments,
            reply=nested,
            message_id=str(data.get("message_id") or ""),
            time=_as_int(data.get("time"), 0),
            title=str(sender.get("title") or ""),
            level=str(sender.get("level") or ""),
        )

    @staticmethod
    def _parse_astr_chain(chain) -> tuple[list[MessageSegment], ReplyMessage | None]:
        segments: list[MessageSegment] = []
        reply: ReplyMessage | None = None
        for comp in chain:
            if isinstance(comp, Plain):
                text = str(getattr(comp, "text", "") or "")
                if text:
                    segments.append(MessageSegment(type="text", text=text))
            elif isinstance(comp, Face):
                face_id = str(getattr(comp, "id", "") or "")
                if face_id.isdigit():
                    segments.append(
                        MessageSegment(type="face", kind="emoji", id=face_id)
                    )
                else:
                    segments.append(MessageSegment(type="text", text="[表情]"))
            elif isinstance(comp, AstrImage):
                url = str(getattr(comp, "url", "") or "")
                file = str(getattr(comp, "file", "") or "")
                target = url or file
                if target:
                    segments.append(
                        MessageSegment(
                            type="image", kind="image", url=target, id=file
                        )
                    )
                else:
                    logger.debug("[reply_guard] 引用链里的图片既没有 url 也没有 file")
            elif isinstance(comp, AstrRecord):
                seconds = str(getattr(comp, "duration", "") or "").strip()
                source = str(
                    getattr(comp, "url", "") or getattr(comp, "file", "") or ""
                ).strip()
                segments.append(
                    MessageSegment(type="record", text=seconds, url=source)
                )
            elif isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                name = str(getattr(comp, "name", "") or "")
                if qq == "all":
                    segments.append(MessageSegment(type="text", text="@全体成员"))
                elif qq:
                    segments.append(MessageSegment(type="at", id=qq, text=name))
            elif isinstance(comp, Reply):
                nested_chain = list(getattr(comp, "chain", None) or [])
                nested_id = str(getattr(comp, "id", "") or "")
                if not nested_chain:
                    reply = ReplyMessage(
                        nickname=str(getattr(comp, "sender_nickname", "") or ""),
                        segments=[
                            MessageSegment(type="text", text="[引用消息不可用]")
                        ],
                        message_id=nested_id,
                    )
                    continue
                nested_segments, nested_reply = ReplyGuardPlugin._parse_astr_chain(
                    nested_chain
                )
                reply = ReplyMessage(
                    nickname=str(getattr(comp, "sender_nickname", "") or ""),
                    segments=nested_segments,
                    reply=nested_reply,
                    message_id=nested_id,
                )
        return segments, reply

    @staticmethod
    def _parse_onebot_message(
        data: dict,
    ) -> tuple[list[MessageSegment], str | None]:
        segments: list[MessageSegment] = []
        reply_id: str | None = None
        raw = data.get("message")
        if raw is None:
            raw = data.get("raw_message")
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return segments, None
            if "[CQ:" in text:
                return parse_cq_string(text)
            segments.append(MessageSegment(type="text", text=text))
            return segments, None

        for seg in raw or []:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                text = str(seg_data.get("text") or "")
                if text:
                    segments.append(MessageSegment(type="text", text=text))
            elif seg_type == "image":
                url = str(seg_data.get("url") or "")
                file = str(seg_data.get("file") or "")
                source = url or file
                if source:
                    sub_type = str(seg_data.get("sub_type") or "")
                    kind = "sticker" if sub_type == "1" else "image"
                    segments.append(
                        MessageSegment(
                            type="image", kind=kind, url=source, id=file
                        )
                    )
                else:
                    logger.debug("[reply_guard] 图片段既没有 url 也没有 file")
            elif seg_type == "face":
                face_id = str(seg_data.get("id") or "")
                if face_id.isdigit():
                    segments.append(MessageSegment(type="face", kind="emoji", id=face_id))
                else:
                    segments.append(MessageSegment(type="text", text="[表情]"))
            elif seg_type == "record":
                seconds = ""
                for key in ("seconds", "duration", "time", "length"):
                    value = seg_data.get(key)
                    if value:
                        seconds = str(value)
                        break
                source = str(seg_data.get("url") or seg_data.get("file") or "")
                segments.append(
                    MessageSegment(type="record", text=seconds, url=source)
                )
            elif seg_type == "at":
                qq = str(seg_data.get("qq") or "")
                if qq:
                    segments.append(MessageSegment(type="at", id=qq))
            elif seg_type == "reply":
                reply_id = str(seg_data.get("id") or "") or None
        return segments, reply_id

    # ------------------------------------------------------------ OneBot

    @staticmethod
    def _get_client(event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        client = getattr(bot, "api", None) or bot
        if hasattr(client, "call_action"):
            return client
        return None

    def _make_image_resolver(self, event: AstrMessageEvent):
        """图片全都没取到时，用 OneBot 的 get_image 换个能读的本地路径。"""
        client = self._get_client(event)
        if client is None:
            return None

        async def resolver(segment: MessageSegment) -> str:
            for candidate in (segment.id, segment.url):
                candidate = str(candidate or "").strip()
                if not candidate:
                    continue
                try:
                    result = await client.call_action("get_image", file=candidate)
                except Exception as e:
                    logger.debug(
                        f"[reply_guard] get_image 调用失败 {candidate[:80]}: {e}"
                    )
                    continue
                data = result.get("data") if isinstance(result, dict) else None
                if isinstance(data, dict):
                    path = str(data.get("file") or data.get("path") or "")
                    if path:
                        logger.debug(f"[reply_guard] get_image 兜底: {path}")
                        return path
            return ""

        return resolver

    async def _fetch_onebot_message(
        self, event: AstrMessageEvent, message_id
    ) -> dict | None:
        if message_id in (None, ""):
            return None
        cached = self._getmsg_cache.get(str(message_id))
        if isinstance(cached, dict):
            return cached
        client = self._get_client(event)
        if client is None:
            return None

        try:
            mid: Any = int(str(message_id))
        except (TypeError, ValueError):
            mid = message_id

        try:
            result = await client.call_action("get_msg", message_id=mid)
        except Exception as e:
            logger.debug(f"[reply_guard] get_msg 调用失败: {e}")
            return None

        if not isinstance(result, dict):
            return None
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        if isinstance(data, dict):
            self._last_getmsg_keys = sorted(data.keys())
            self._getmsg_cache[str(message_id)] = data
            if len(self._getmsg_cache) > 50:
                self._getmsg_cache.pop(next(iter(self._getmsg_cache)))
        return data if isinstance(data, dict) else None
