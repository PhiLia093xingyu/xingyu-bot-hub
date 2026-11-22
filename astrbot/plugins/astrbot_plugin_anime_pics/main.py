"""二次元搜图：pixiv / yande.re / konachan，走本机代理，图直接发到会话里。

给 AstrBot 的 agent 提供一个 `anime_pic_search` 工具，另带 `/搜图` 手动命令。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType

try:  # AstrBot 以 data.plugins.<dir>.main 的形式加载
    from . import sources
except ImportError:  # 单独跑测试脚本时
    import sources  # type: ignore[no-redef]

SOURCE_BASES = {
    "yande": "https://yande.re",
    "konachan": "https://konachan.com",
}

DEFAULTS = {
    "proxy": "http://127.0.0.1:7890",
    "pixiv_cookie": "",
    "enable_pixiv": True,
    "enable_yande": True,
    "enable_konachan": False,
    "enable_bing": True,
    "default_count": 3,
    "max_count": 8,
    "safe_mode": True,
    "search_order": "popular_d",
    "exclude_ai": True,
    "ja_tags": True,
    "dedup_hours": 6,
    "auto_intercept": True,
    "intercept_count": 2,
    "timeout": 20,
    "verify_ssl": True,
    "cooldown": 5,
    "cache_days": 3,
}


@register(
    "astrbot_plugin_anime_pics",
    "<BOT_NAME>",
    "二次元搜图：pixiv / yande.re，走代理抓图并直接发到会话",
    "v0.1.0",
    "",
)
class AnimePicsPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_anime_pics")
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_call: dict[str, float] = {}
        self._sent: dict[str, float] = {}
        self._hint_logged = 0
        self._proxy_dead_until = 0.0

    # ------------------------------------------------------------ config
    def _cfg(self, key: str):
        value = None
        if hasattr(self.config, "get"):
            value = self.config.get(key)
        return DEFAULTS[key] if value is None else value

    @property
    def proxy(self) -> str:
        return str(self._cfg("proxy") or "").strip()

    def _sources(self, wanted: str) -> list[str]:
        wanted = (wanted or "auto").strip().lower()
        if wanted == "bing":
            return ["bing"]
        if wanted in ("pixiv", "yande", "konachan"):
            return [wanted]
        order = []
        if self._cfg("enable_pixiv"):
            order.append("pixiv")
        if self._cfg("enable_yande"):
            order.append("yande")
        if self._cfg("enable_konachan"):
            order.append("konachan")
        if self._cfg("enable_bing"):
            order.append("bing")  # 国内直连，放最后兜底
        return order or ["pixiv"]

    # ------------------------------------------------------------ search
    def _search_one(self, src: str, keyword: str, limit: int, safe: bool) -> list[sources.Pic]:
        timeout = float(self._cfg("timeout"))
        verify = bool(self._cfg("verify_ssl"))
        if src == "pixiv":
            variants = [keyword]
            if self._cfg("ja_tags"):
                ja = sources.to_ja_tags(keyword)
                if ja and ja != keyword:
                    variants.append(ja)
            merged: list[sources.Pic] = []
            seen: set[str] = set()
            last_error: Exception | None = None
            for query in variants:
                try:
                    pics = sources.search_pixiv(
                        query,
                        limit=max(1, limit - len(merged)),
                        proxy=self.proxy,
                        cookie=str(self._cfg("pixiv_cookie") or ""),
                        safe=safe,
                        timeout=timeout,
                        verify=verify,
                        order=str(self._cfg("search_order") or "popular_d"),
                        exclude_ai=bool(self._cfg("exclude_ai")),
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    logger.warning(f"[anime_pics] pixiv 查询 {query!r} 失败: {exc}")
                    continue
                for pic in pics:
                    if pic.id in seen:
                        continue
                    seen.add(pic.id)
                    merged.append(pic)
                if len(merged) >= limit:
                    break
            if not merged and last_error is not None:
                raise last_error
            return merged
        base = SOURCE_BASES.get(src)
        if base:
            return sources.search_booru(
                base,
                keyword,
                limit=limit,
                proxy=self.proxy,
                safe=safe,
                timeout=timeout,
                verify=verify,
            )
        if src == "bing":
            return sources.search_bing(keyword, limit=limit, timeout=timeout, verify=verify)
        raise sources.SourceError(f"未知图源 {src}")

    def _proxy_alive(self) -> bool:
        """快速探一下代理能不能出网；失败后 5 分钟内只走国内源。"""
        if not self.proxy:
            return True
        now = time.time()
        if now < self._proxy_dead_until:
            return False
        import httpx

        try:
            with httpx.Client(
                proxy=self.proxy,
                timeout=5,
                verify=False,
                headers={"User-Agent": sources.UA, "Connection": "close"},
            ) as client:
                resp = client.get("https://www.gstatic.com/generate_204")
            return resp.status_code in (200, 204)
        except Exception:  # noqa: BLE001
            self._proxy_dead_until = now + 60
            logger.warning("[anime_pics] 代理探测失败，60 秒内只走国内源（bing）")
            return False

    def _search_sync(
        self, keyword: str, source: str, limit: int, safe: bool
    ) -> tuple[list[sources.Pic], str, list[str]]:
        errors: list[str] = []
        collected: list[sources.Pic] = []
        seen: set[tuple[str, str]] = set()
        order = self._sources(source)
        if self.proxy and not self._proxy_alive():
            order = [src for src in order if src == "bing"]
            errors.append("代理不通，已跳过 pixiv/yande")
            if not order:
                return [], "", errors
        for src in order:
            for attempt in range(2):  # 代理偶发抖动，同一图源再试一次
                try:
                    pics = self._search_one(
                        src, keyword, max(limit - len(collected), 1), safe
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{src}: {exc}")
                    logger.warning(f"[anime_pics] {src} 第 {attempt + 1} 次搜索失败: {exc}")
                    if attempt == 0:
                        time.sleep(1.0)
                    continue
                new = 0
                for pic in pics:
                    key = (pic.source, pic.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(pic)
                    new += 1
                    if len(collected) >= limit:
                        break
                if not new:
                    errors.append(f"{src}: 没有结果")
                break
            if len(collected) >= limit:
                break
        used = "+".join(sorted({pic.source for pic in collected})) if collected else ""
        return collected, used, errors

    def _download_sync(self, pics: list[sources.Pic]) -> list[sources.Pic]:
        timeout = float(self._cfg("timeout"))
        verify = bool(self._cfg("verify_ssl"))
        day_dir = self.cache_dir / time.strftime("%Y%m%d")
        ok: list[sources.Pic] = []
        for pic in pics:
            try:
                sources.download_image(
                    pic,
                    str(day_dir),
                    proxy=self.proxy if pic.use_proxy else "",
                    timeout=max(30.0, timeout),
                    verify=verify,
                )
                ok.append(pic)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[anime_pics] 下载 {pic.image_url} 失败: {exc}")
        return ok

    def _cleanup_cache(self) -> None:
        try:
            keep_days = int(self._cfg("cache_days"))
        except (TypeError, ValueError):
            keep_days = 3
        cutoff = time.time() - max(1, keep_days) * 86400
        for entry in self.cache_dir.iterdir():
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    for f in entry.iterdir():
                        f.unlink(missing_ok=True)
                    entry.rmdir()
            except OSError:
                pass

    def _cooldown_left(self, key: str) -> float:
        try:
            cooldown = float(self._cfg("cooldown"))
        except (TypeError, ValueError):
            cooldown = 5.0
        left = self._last_call.get(key, 0.0) + cooldown - time.time()
        return left if left > 0 else 0.0

    def _dedup_ttl(self) -> float:
        try:
            return max(0.0, float(self._cfg("dedup_hours"))) * 3600
        except (TypeError, ValueError):
            return 6 * 3600

    def _drop_recent(self, pics: list[sources.Pic]) -> list[sources.Pic]:
        """把最近发过的图过滤掉（全部中招时返回空，由调用方决定放宽还是放弃）。"""
        ttl = self._dedup_ttl()
        if ttl <= 0:
            return pics
        now = time.time()
        self._sent = {k: v for k, v in self._sent.items() if now - v < ttl}
        return [p for p in pics if f"{p.source}:{p.id}" not in self._sent]

    def _mark_sent(self, pics: list[sources.Pic]) -> None:
        now = time.time()
        for pic in pics:
            self._sent[f"{pic.source}:{pic.id}"] = now

    # ------------------------------------------------------------ 兜底拦截
    IMAGE_REQ_RE = re.compile(r"(找|搜|来|发|要|整|看看|帮我).{0,10}(图|图片|插画|壁纸)")
    STOP_WORDS = (
        "<BOT_NAME>", "pixiv", "Pixiv", "PIXIV", "帮我", "帮忙", "再", "试试", "找", "搜",
        "来", "发", "要", "整", "看看", "一张", "两张", "三张", "四张", "五张",
        "张", "的图", "图片", "插画", "壁纸", "一下", "呗", "嘛", "呀", "吧", "二次元",
        "图", "说不定", "能不能", "你的", "我的", "能不能", "吗", "呢", "了",
    )

    def _extract_keyword(self, text: str) -> str:
        out = re.sub(r"[（(【\[].*?[)）】\]]", " ", text)  # 去掉括号里的碎话
        for word in self.STOP_WORDS:
            out = out.replace(word, " ")
        out = re.sub(r"[^\w\u4e00-\u9fff]+", " ", out)
        tokens = [tok.strip("的了着啊呀吧") for tok in out.split()]
        tokens = [tok for tok in tokens if len(tok) >= 2]
        tokens = list(dict.fromkeys(tokens))  # 原始文本和处理后文本可能重复
        return " ".join(tokens[:2])[:24]

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def auto_image_request(self, event: AstrMessageEvent):
        """群里 @ 它 / 叫它名字要图时，直接搜图发出去，不再指望模型自己选工具。"""
        if not bool(self._cfg("auto_intercept")):
            return
        text = (event.message_str or "").strip()
        # AstrBot 会把唤醒词（比如"<BOT_NAME>"）从 message_str 里剥掉，原始文本要另外拿
        msg_obj = getattr(event, "message_obj", None)
        raw = (getattr(msg_obj, "message_str", "") or "").strip() if msg_obj else ""
        if raw and raw not in text:
            text = f"{raw} {text}".strip()
        if not text or len(text) > 80 or text.startswith("/"):
            return
        if event.get_sender_id() == event.get_self_id():
            return
        if not (
            self.IMAGE_REQ_RE.search(text)
            or ("pixiv" in text.lower() and "图" in text)
            or ("的图" in text and len(text) <= 20)
        ):
            return

        keyword = self._extract_keyword(text)
        if not keyword:
            return

        session_key = getattr(event, "unified_msg_origin", "") or "global"
        wait = self._cooldown_left(session_key)
        if wait > 0:
            return

        try:
            count = int(self._cfg("intercept_count"))
        except (TypeError, ValueError):
            count = 2
        count = max(1, min(count, 4))

        logger.info("[anime_pics] 拦截到要图请求，关键词=%r", keyword)
        loop = asyncio.get_running_loop()
        pics, used, errors = await loop.run_in_executor(
            None, self._search_sync, keyword, "auto", count, True
        )
        if not pics:
            logger.warning("[anime_pics] 拦截搜索失败：%s", errors)
            if self.proxy and not self._proxy_alive():
                # 用户明确要求：宁可不发，也不发国内源的图
                await event.send(
                    event.chain_result([Plain("pixiv 这会儿连不上（代理线路不通），等会儿再喊我～")])
                )
                event.stop_event()
            return
        pics = self._drop_recent(pics)[:count] or pics[:count]
        downloaded = await loop.run_in_executor(None, self._download_sync, pics)
        if not downloaded:
            return

        self._last_call[session_key] = time.time()
        self._mark_sent(downloaded)
        await loop.run_in_executor(None, self._cleanup_cache)
        caption = f"喏，{keyword}。" if used != "bing" else f"喏，{keyword}。（pixiv 这会儿出不去，这张是国内图源搜的）"
        chain = [Plain(caption)] + [
            Image.fromFileSystem(pic.local_path) for pic in downloaded
        ]
        try:
            await event.send(event.chain_result(chain))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[anime_pics] 图片发送失败（多半是 VPN 把 QQ 的上传流量也代理了）: %s", exc
            )
            try:
                await event.send(
                    event.chain_result(
                        [Plain("图搜到了，但发不出去——VPN 好像把 QQ 的图片上传也走了隧道，检查一下分流设置。")]
                    )
                )
            except Exception:  # noqa: BLE001
                pass
            event.stop_event()
            return
        event.stop_event()
        logger.info("[anime_pics] 已直发 %d 张（源=%s）", len(downloaded), used)

    # ------------------------------------------------------------ prompt
    @filter.on_llm_request()
    async def inject_image_tool_hint(self, event: AstrMessageEvent, req) -> None:
        """在系统提示里钉一条硬规则：要图就用本工具，别自己写脚本抓。"""
        hint = (
            "【工具规则】用户想要二次元图片/插画/壁纸，或者提到 pixiv、要找某个角色（例如希儿）的图时，"
            "必须调用 anime_pic_search 工具完成——它内部走本地代理、带 pixiv 登录态、已过滤 R18 和 AI 图。"
            "不要用 astrbot_execute_python / shell 自己写请求去抓图：本机直连 pixiv、danbooru 等境外站点是不通的。"
        )
        current = getattr(req, "system_prompt", "") or ""
        if "【工具规则】" in current:
            return
        req.system_prompt = current + "\n\n" + hint
        if self._hint_logged < 3:
            self._hint_logged += 1
            names = [t.name for t in (getattr(getattr(req, "func_tool", None), "tools", []) or [])]
            logger.info(
                "[anime_pics] 已注入工具规则；本轮工具 %d 个，含 anime_pic_search=%s",
                len(names),
                "anime_pic_search" in names,
            )

    # ------------------------------------------------------------ tool
    @filter.llm_tool(name="anime_pic_search")
    async def anime_pic_search(
        self,
        event: AstrMessageEvent,
        keyword: str,
        source: str = "auto",
        count: int = 0,
        safe: bool = True,
    ):
        """搜索二次元插画/壁纸，并把图片直接发到当前会话。

        当用户要二次元的图（插画、壁纸、猫耳、白毛、泳装、某个动漫角色/游戏角色），
        或者提到 pixiv、发图、来张图、"找XX的图"时，必须调用本工具。

        不要用 astrbot_execute_python 或 shell 自己写请求去抓 pixiv/yande：
        本机直连境外站点是不通的，本工具内部会自动走本地代理、带登录态、过滤 R18 和 AI 图。
        图片会由本工具自动发出，你只需要用一句话点评/介绍，不要再用 send_message_to_user 重发。

        Args:
            keyword(string): 搜索关键词，中/英/日文均可，例如"猫耳 橙发""白发 少女""希儿"
            source(string): 图源，可选 auto / pixiv / yande / konachan，默认 auto（先 pixiv 再 yande）
            count(number): 要几张图，0 表示用配置里的默认值
            safe(boolean): 是否只要全年龄内容，默认 true
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return json.dumps({"ok": False, "error": "关键词是空的"}, ensure_ascii=False)

        try:
            default_count = int(self._cfg("default_count"))
            max_count = int(self._cfg("max_count"))
        except (TypeError, ValueError):
            default_count, max_count = 3, 8
        limit = int(count) if count else default_count
        limit = max(1, min(limit, max(1, max_count), 8))
        safe = bool(safe) and bool(self._cfg("safe_mode"))

        session_key = getattr(event, "unified_msg_origin", "") or "global"
        wait = self._cooldown_left(session_key)
        if wait > 0:
            return json.dumps(
                {"ok": False, "error": f"刚搜过，{wait:.0f} 秒后再来"},
                ensure_ascii=False,
            )

        loop = asyncio.get_running_loop()
        pics, used, errors = await loop.run_in_executor(
            None, self._search_sync, keyword, source, limit, safe
        )
        if not pics:
            hint = ""
            if any(("Connect" in e or "SSL" in e or "Timeout" in e) for e in errors):
                hint = "（线路不通：境外站点连不上，先检查 iKuuu 代理是否还连着）"
            return json.dumps(
                {"ok": False, "keyword": keyword, "error": "没搜到图" + hint, "detail": errors},
                ensure_ascii=False,
            )

        pics = self._drop_recent(pics)[:limit]
        if not pics:
            wider = min(max(limit * 4, limit + 6), 20)
            more, used_wider, errors_wider = await loop.run_in_executor(
                None, self._search_sync, keyword, source, wider, safe
            )
            pics = self._drop_recent(more)[:limit]
            errors.extend(errors_wider)
            if used_wider:
                used = used_wider
            if not pics:
                return json.dumps(
                    {
                        "ok": False,
                        "keyword": keyword,
                        "error": "这些图最近都发过了，换个关键词或者等一会儿再试",
                    },
                    ensure_ascii=False,
                )
        self._last_call[session_key] = time.time()
        downloaded = await loop.run_in_executor(None, self._download_sync, pics)
        await loop.run_in_executor(None, self._cleanup_cache)

        if not downloaded:
            return json.dumps(
                {"ok": False, "keyword": keyword, "source": used, "error": "搜到了但一张都没下下来", "detail": errors},
                ensure_ascii=False,
            )

        chain = [Image.fromFileSystem(pic.local_path) for pic in downloaded]
        try:
            await event.send(event.chain_result(chain))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[anime_pics] 发送图片失败: {exc}")
            return json.dumps(
                {
                    "ok": False,
                    "error": "图搜到了但发送失败（VPN 可能把 QQ 的图片上传也代理了，检查分流）",
                },
                ensure_ascii=False,
            )

        payload = {
            "ok": True,
            "keyword": keyword,
            "source": used,
            "sent": len(downloaded),
            "note": "图片已经发出去了，直接用一句话介绍即可，不要重复发送。",
            "images": [pic.to_dict() for pic in downloaded],
        }
        self._mark_sent(downloaded)
        if errors:
            payload["fallbacks"] = errors
        return json.dumps(payload, ensure_ascii=False)

    # ------------------------------------------------------------ command
    @filter.command("搜图")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        """手动搜图：/搜图 猫耳 橙发"""
        keyword = (keyword or "").strip() or (event.message_str or "").replace("/搜图", "").strip()
        if not keyword:
            yield event.plain_result("用法：/搜图 猫耳 橙发（中英日文都行）")
            return

        session_key = getattr(event, "unified_msg_origin", "") or event.get_sender_id()
        wait = self._cooldown_left(session_key)
        if wait > 0:
            yield event.plain_result(f"刚搜过，{wait:.0f} 秒后再试。")
            return

        try:
            default_count = int(self._cfg("default_count"))
            max_count = int(self._cfg("max_count"))
        except (TypeError, ValueError):
            default_count, max_count = 3, 8
        limit = max(1, min(default_count, max(1, max_count), 8))

        loop = asyncio.get_running_loop()
        pics, used, errors = await loop.run_in_executor(
            None, self._search_sync, keyword, "auto", limit, bool(self._cfg("safe_mode"))
        )
        if not pics:
            detail = "；".join(errors[:3])
            yield event.plain_result(f"没搜到「{keyword}」。{detail}")
            return

        pics = self._drop_recent(pics)[:limit]
        if not pics:
            yield event.plain_result(f"「{keyword}」这些图最近都发过了，换个词试试？")
            return
        self._last_call[session_key] = time.time()
        downloaded = await loop.run_in_executor(None, self._download_sync, pics)
        if not downloaded:
            yield event.plain_result(f"搜到 {len(pics)} 张「{keyword}」，但一张都没下下来（线路不稳？）")
            return

        await loop.run_in_executor(None, self._cleanup_cache)
        self._mark_sent(downloaded)
        yield event.chain_result([Image.fromFileSystem(pic.local_path) for pic in downloaded])

    def _health(self, url: str, timeout: float = 8.0) -> str:
        import httpx

        try:
            kwargs = {
                "timeout": timeout,
                "verify": bool(self._cfg("verify_ssl")),
                "headers": {"User-Agent": sources.UA, "Connection": "close"},
                "follow_redirects": True,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            with httpx.Client(**kwargs) as client:
                resp = client.get(url)
            return f"OK {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"失败（{type(exc).__name__}）"

    @filter.command("搜图状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """看一眼搜图插件的配置和线路状态：/搜图状态"""
        cookie = str(self._cfg("pixiv_cookie") or "").strip()
        loop = asyncio.get_running_loop()
        pixiv = await loop.run_in_executor(None, self._health, "https://www.pixiv.net/")
        yande = await loop.run_in_executor(
            None, self._health, "https://yande.re/post.json?tags=nekomimi&limit=1"
        )
        lines = [
            f"图源：{'、'.join(self._sources('auto'))}",
            f"代理：{self.proxy or '（直连）'}",
            f"pixiv 连通：{pixiv}",
            f"yande 连通：{yande}",
            f"pixiv cookie：{'已配置（' + str(len(cookie)) + ' 字符）' if cookie else '未配置（也能搜，只是结果少一些）'}",
            f"默认张数：{self._cfg('default_count')}，上限 {self._cfg('max_count')}，全年龄：{self._cfg('safe_mode')}",
        ]
        if "失败" in pixiv and "失败" in yande:
            lines.append("两个都不通 —— 多半是代理节点掉了，去 iKuuu 客户端点一下重连。")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        self._last_call.clear()
