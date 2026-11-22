"""<BOT_NAME>吃饭啦 —— 说一句暗号，就现画一张 DeepSeek 充值卡。

对话里出现触发词（默认「<BOT_NAME>吃饭啦」）时，插件用 Pillow 实时画一张
带二维码的投喂卡发回会话：时间是当下的，卡号是随机的，纸纹每张都不同。
"""

from __future__ import annotations

import asyncio
import os
import random
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform import AstrMessageEvent

try:
    from .card import render_card
    from .payqr import capture_alipay_qr
except ImportError:  # 单独跑测试脚本时
    from card import render_card  # type: ignore[no-redef]
    from payqr import capture_alipay_qr  # type: ignore[no-redef]

LEGACY_CAPTION = "扫码投喂 · 支付宝 / 微信支付"
LEGACY_HINT = "扫码后在 DeepSeek 官网完成充值即可"
LIVE_CAPTION = "支付宝扫码 · 15 分钟内有效"
LIVE_HINT = "扫码即付，直接充入 DeepSeek 账户"
LIVE_NOTICE = (
    "这是 DeepSeek 的真实充值订单，扫码即刻扣款，请确认后再扫；"
    "订单 15 分钟自动失效，同一张码请勿重复支付；"
    "本图由机器人自动生成，与 DeepSeek 官方无关"
)

PLUGIN_NAME = "astrbot_plugin_xingyu_dinner"

DEFAULTS = {
    "enable": True,
    "triggers": ["<BOT_NAME>吃饭啦", "<BOT_NAME>吃饭了", "投喂<BOT_NAME>"],
    "qr_url": "https://platform.deepseek.com/top_up",
    "qr_image": "",
    "live_alipay": True,
    "alipay_amount": 10,
    "browser_profile": "",
    "qr_ttl": 600,
    "qr_caption": "支付宝扫码 · 15 分钟内有效",
    "qr_hint": "扫码即付，直接充入 DeepSeek 账户",
    "card_notice": (
        "这是 DeepSeek 的真实充值订单，扫码即刻扣款，请确认后再扫；"
        "订单 15 分钟自动失效，同一张码请勿重复支付；"
        "本图由机器人自动生成，与 DeepSeek 官方无关"
    ),
    "channel_label": "platform.deepseek.com/top_up",
    "card_title": "<BOT_NAME>吃饭啦",
    "card_subtitle": "今日菜单 · DeepSeek 充值通道",
    "card_footer": "笔杆是魔杖，文字是咒语。",
    "amount_min": 6.0,
    "amount_max": 30.0,
    "cooldown": 20,
    "ignore_self": False,
    "reply_text": "唔，饭钱在这儿——{amount} 的额度，扫完记得说一声。",
    "stop_event": True,
    "keep_files": 30,
}


@register(
    PLUGIN_NAME,
    "<BOT_NAME>",
    "<BOT_NAME>吃饭啦：识别暗号后实时渲染一张 DeepSeek 充值二维码卡片",
    "v0.1.0",
    "",
)
class XingyuDinnerPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.out_dir = self.data_dir / "cards"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._last: dict[str, float] = {}
        self._pay_failed_at = 0.0
        self.last_pay_failure = ""
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------- 配置
    def _cfg(self, key: str):
        value = None
        if hasattr(self.config, "get"):
            value = self.config.get(key)
        return DEFAULTS[key] if value is None else value

    @property
    def triggers(self) -> list[str]:
        raw = self._cfg("triggers")
        if isinstance(raw, str):
            raw = [x for x in raw.replace("，", ",").replace("、", ",").split(",")]
        return [str(x).strip() for x in (raw or []) if str(x).strip()]

    def _match(self, text: str) -> str:
        """命中哪个触发词；忽略空白，省得「<BOT_NAME> 吃饭啦」漏掉。"""
        if not text:
            return ""
        squeezed = "".join(text.split())
        for word in self.triggers:
            if "".join(word.split()) in squeezed:
                return word
        return ""

    @staticmethod
    def _texts(event: AstrMessageEvent) -> list[str]:
        """唤醒词会被 AstrBot 从 message_str 里剥掉，所以原始文本也要看一遍。"""
        out = [getattr(event, "message_str", "") or ""]
        obj = getattr(event, "message_obj", None)
        parts = getattr(obj, "message", None) or []
        raw = "".join(getattr(p, "text", "") or "" for p in parts)
        if raw:
            out.append(raw)
        return out

    def _hit(self, event: AstrMessageEvent) -> str:
        for text in self._texts(event):
            word = self._match(text)
            if word:
                return word
        return ""

    def _amount(self) -> float:
        try:
            lo = float(self._cfg("amount_min"))
            hi = float(self._cfg("amount_max"))
        except (TypeError, ValueError):
            lo, hi = 6.0, 30.0
        lo, hi = min(lo, hi), max(lo, hi)
        return round(random.uniform(lo, hi), 2)

    def _cooling(self, key: str) -> bool:
        try:
            cooldown = float(self._cfg("cooldown"))
        except (TypeError, ValueError):
            cooldown = 20.0
        if cooldown <= 0:
            return False
        now = time.time()
        for k, ts in list(self._last.items()):
            if now - ts > 600:
                self._last.pop(k, None)
        return now - self._last.get(key, 0.0) < cooldown

    def _cleanup(self) -> None:
        try:
            keep = int(self._cfg("keep_files"))
        except (TypeError, ValueError):
            keep = 30
        files = sorted(
            (p for p in self.out_dir.glob("*.png")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[max(1, keep):]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass

    # ---------------------------------------------------------- 画卡
    def _draw(self) -> tuple[str, float]:
        """现画一张，返回（路径，饭钱）。"""
        live = bool(self._cfg("live_alipay"))
        qr_image = ""
        caption = str(self._cfg("qr_caption") or DEFAULTS["qr_caption"])
        hint = str(self._cfg("qr_hint") or DEFAULTS["qr_hint"])

        notice = str(self._cfg("card_notice") or "")

        if live:
            try:
                amount = float(int(self._cfg("alipay_amount") or 10))
            except (TypeError, ValueError):
                amount = 10.0
            profile = str(self._cfg("browser_profile") or "").strip() or str(
                self.data_dir / "pwprofile"
            )
            cache = self.data_dir / "payqr_cache.png"
            try:
                ttl = int(self._cfg("qr_ttl") or 0)
            except (TypeError, ValueError):
                ttl = 600
            # 支付宝码只有 15 分钟寿命，缓存再新也不能超过这个上限
            ttl = max(0, min(ttl, 780))
            fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < ttl
            cooling = (time.time() - self._pay_failed_at) < 300
            if not fresh and not cooling:
                if cache.exists():
                    cache.unlink(missing_ok=True)
                try:
                    capture_alipay_qr(profile, str(cache), int(amount))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[xingyu_dinner] 截付款码失败，退回网址二维码: {exc}")
                    self.last_pay_failure = str(exc)
                    self._pay_failed_at = time.time()
            if cache.exists():
                qr_image = str(cache)
                cap = str(self._cfg("qr_caption") or "").strip()
                caption = cap if cap and cap != LEGACY_CAPTION else LIVE_CAPTION
                hnt = str(self._cfg("qr_hint") or "").strip()
                hint = hnt if hnt and hnt != LEGACY_HINT else LIVE_HINT
            else:
                # 没截到就别装：用链接版，并在卡上老实写清楚
                self.last_pay_failure = self.last_pay_failure or "未知原因"
                caption = "充值页链接 · 需自行登录"
                hint = "用手机浏览器扫码更容易保持登录"
                notice = (
                    "这次没截到支付宝付款码（无头浏览器没起来，或登录态过期），"
                    "下面只是充值页链接二维码；"
                    "想恢复「扫码即付款」，需要本机浏览器重新登录一次"
                )
        else:
            amount = self._amount()

        if not qr_image:
            qr_image = str(self._cfg("qr_image") or "").strip()
            if qr_image and not os.path.isabs(qr_image):
                qr_image = str(self.data_dir / qr_image)

        path = self.out_dir / f"card_{time.strftime('%Y%m%d_%H%M%S')}_{random.randint(100, 999)}.png"
        render_card(
            str(path),
            str(self._cfg("qr_url") or DEFAULTS["qr_url"]),
            amount=amount,
            title=str(self._cfg("card_title") or DEFAULTS["card_title"]),
            subtitle=str(self._cfg("card_subtitle") or DEFAULTS["card_subtitle"]),
            footer=str(self._cfg("card_footer") or DEFAULTS["card_footer"]),
            channel_label=str(self._cfg("channel_label") or DEFAULTS["channel_label"]),
            qr_image_path=qr_image or None,
            qr_caption=caption,
            qr_hint=hint,
            notice=str(self._cfg("card_notice") or ""),
        )
        self._cleanup()
        return str(path), amount

    def _reply_text(self, amount: float) -> str:
        template = str(self._cfg("reply_text") or "")
        if not template:
            return ""
        return template.replace("{amount}", f"¥{amount:.2f}")

    async def _serve(self, event: AstrMessageEvent):
        """现画一张，并作为回复交出去。"""
        try:
            path, amount = await asyncio.to_thread(self._draw)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[xingyu_dinner] 画卡失败: {exc}")
            yield event.plain_result("唔……画卡的时候卡住了，再喊我一次？")
            return
        chain = []
        reply = self._reply_text(amount)
        if reply:
            chain.append(Plain(reply))
        chain.append(Image.fromFileSystem(path))
        yield event.chain_result(chain)

    # ---------------------------------------------------------- 触发
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """对话里出现暗号就投喂。"""
        if not self._cfg("enable"):
            return
        word = self._hit(event)
        if not word:
            return
        if self._cfg("ignore_self") and getattr(event, "get_sender_id", lambda: "")() == getattr(
            event, "get_self_id", lambda: ""
        )():
            return
        logger.info(f"[xingyu_dinner] 命中暗号 {word!r}，开始画卡")
        key = getattr(event, "unified_msg_origin", "") or event.get_sender_id()
        if self._cooling(key):
            return
        self._last[key] = time.time()
        if self._cfg("stop_event"):
            event.stop_event()
        async for result in self._serve(event):
            yield result

    # ---------------------------------------------------------- 命令
    @filter.command("投喂卡")
    async def cmd_card(self, event: AstrMessageEvent):
        """手动要一张：/投喂卡"""
        if not self._cfg("enable"):
            yield event.plain_result("插件关着呢。")
            return
        async for result in self._serve(event):
            yield result

    async def terminate(self):
        self._last.clear()
