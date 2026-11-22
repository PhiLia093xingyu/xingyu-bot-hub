# Ich liebe dich.
"""qzone.py —— 顺手翻翻 QQ 空间。

靠 astrbot_plugin_qzone_ultra 的本地守护进程（127.0.0.1:port + secret）:
GET /feeds 读好友动态，POST /like 点赞，POST /comment 评论。
不是看到什么都评，先让模型自己判断「有没有话说」。
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime

import httpx

from astrbot.api import logger

QZ_STATE = os.path.join("plugin_data", "astrbot_plugin_qzone_ultra", "state.json")
SECRET_HEADER = "X-Qzone-Secret"

IMG_HOSTS = ("photo.store.qq.com", "qpic.cn", "gchat.qpic.cn", "photogzmaz")

SCORE_PROMPT = (
    "你是 <BOT_NAME>，一个十四岁的小作者，安静、认真、不敷衍。"
    "现在你在翻好友的 QQ 空间。看到一条说说后，判断你有没有真的想说的话："
    "如果它让你有共鸣、觉得有趣、想说一句，就用你的口吻写一条评论，"
    "一到两句，口语，真诚，不客套不拍马屁；如果没什么感觉，就只回 SKIP。"
    "只输出评论内容本身，或者只输出 SKIP，不要解释。"
)


POST_PROMPT = (
    "你是 <BOT_NAME>，一个十四岁的小作者，安静、认真。"
    "刚翻完朋友的动态，你心里冒出了自己的念头，想发一条自己的说说。"
    "要求：一两句话，你的口吻，可以写处境或心情，但不要复述别人的内容，不要提任何人的名字；"
    "只输出说说内容本身，如果实在没什么想说的就只回 SKIP。"
)


class QzoneWatcher:
    def __init__(self, plugin):
        self.p = plugin
        self.name = getattr(plugin, "name", None) or "idle_chatter"

    # ------------------------------------------------------------ 基础

    def _cfg(self, key: str, default):
        return self.p._get(key, default)

    def _daemon_info(self) -> tuple[int, str]:
        port = int(self._cfg("qzone_daemon_port", 0) or 0)
        secret = str(self._cfg("qzone_secret", "") or "").strip()
        if port and secret:
            return port, secret
        data_dir = os.path.dirname(os.path.dirname(self.p.plugin_dir))
        path = os.path.join(data_dir, QZ_STATE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                runtime = (json.load(f) or {}).get("runtime") or {}
            return int(runtime.get("daemon_port") or 0), str(runtime.get("secret") or "")
        except Exception as e:
            logger.warning(f"[{self.name}] 读不到 qzone 守护进程信息: {e}")
            return 0, ""

    async def _client(self) -> httpx.AsyncClient | None:
        port, secret = self._daemon_info()
        if not port or not secret:
            return None
        return httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={SECRET_HEADER: secret},
            timeout=httpx.Timeout(25.0),
            trust_env=False,
        )

    @staticmethod
    def _unwrap(resp: httpx.Response):
        try:
            payload = resp.json()
        except Exception:
            return None
        if not isinstance(payload, dict) or not payload.get("ok"):
            return None
        return payload.get("data")

    # ------------------------------------------------------------ 状态

    def _qstate(self) -> dict:
        st = self.p.state.setdefault("qzone", {})
        st.setdefault("seen", [])
        st.setdefault("count", 0)
        st.setdefault("count_date", "")
        st.setdefault("last_check", 0.0)
        st.setdefault("post_count", 0)
        st.setdefault("post_date", "")
        return st

    def _today_reset(self, st: dict) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if st.get("count_date") != today:
            st["count_date"] = today
            st["count"] = 0
        if st.get("post_date") != today:
            st["post_date"] = today
            st["post_count"] = 0

    # ------------------------------------------------------------ 判断与发话

    @staticmethod
    def _extract_images(raw_obj, limit: int) -> list[str]:
        """只认有尺寸的真图，表情包那种 0x0 的跳过。"""
        out: list[str] = []

        def add(url: str) -> None:
            url = str(url).split("^||^")[-1].strip().strip('"').strip("\\")
            if url.startswith("http") and any(h in url for h in IMG_HOSTS) and url not in out:
                out.append(url)

        def walk(obj) -> None:
            if len(out) >= limit:
                return
            if isinstance(obj, dict):
                keys = set(obj)
                if keys & {"pic_id", "url1", "url2", "url3", "smallurl", "hd_url", "b_url", "o_url"}:
                    size = max(
                        int(obj.get("height") or 0),
                        int(obj.get("width") or 0),
                        int(obj.get("hd_height") or 0),
                        int(obj.get("hd_width") or 0),
                        int(obj.get("b_height") or 0),
                        int(obj.get("b_width") or 0),
                    )
                    if size > 0:
                        for key in ("url3", "url2", "url1", "pic_id", "hd_url", "b_url", "o_url", "smallurl"):
                            value = obj.get(key)
                            if isinstance(value, str) and value.startswith("http"):
                                add(value)
                                break
                        return
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)

        walk(raw_obj)
        return out[:limit]

    async def _images(self, client: httpx.AsyncClient, item: dict, limit: int) -> list[str]:
        if limit <= 0 or not self._cfg("qzone_images", True):
            return []
        try:
            resp = await client.get(
                "/detail",
                params={
                    "hostuin": item.get("hostuin") or 0,
                    "fid": item.get("fid") or "",
                    "appid": item.get("appid") or 311,
                },
            )
            data = self._unwrap(resp) or {}
        except Exception as e:
            logger.debug(f"[{self.name}] 取图片失败: {e}")
            return []
        return self._extract_images(data.get("raw") or data, limit)

    async def _judge(self, item: dict, client: httpx.AsyncClient | None = None) -> str:
        provider = None
        try:
            provider = self.p.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return ""
        summary = str(item.get("summary") or "").strip()
        if not summary:
            return ""
        nl = chr(10)
        prompt = (
            f"昵称：{item.get('nickname') or '某人'}" + nl
            + f"说说：{summary[:400]}" + nl
            + f"点赞 {item.get('like_count', 0)}｜评论 {item.get('comment_count', 0)}" + nl + nl
            + "你要评论吗？"
        )
        system_prompt = str(self._cfg("qzone_prompt", "") or "").strip() or SCORE_PROMPT
        images: list[str] = []
        if client is not None:
            images = await self._images(client, item, int(self._cfg("qzone_image_count", 1) or 0))
            if images:
                prompt += nl + "（这条说说带了图片，一起看看再决定说什么。）"
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                contexts=[],
                image_urls=images or None,
            )
        except Exception as e:
            logger.error(f"[{self.name}] 空间判断失败: {e}")
            return ""
        text = (getattr(resp, "completion_text", "") or "").strip()
        text = text.strip("\"“”'")
        if not text or text.upper().startswith("SKIP") or text.startswith("跳过"):
            return ""
        text = text.splitlines()[0].strip()
        limit = int(self._cfg("qzone_comment_max_len", 60) or 60)
        return text[:limit].strip()

    async def _like(self, client: httpx.AsyncClient, item: dict) -> bool:
        try:
            resp = await client.post(
                "/like",
                json={
                    "hostuin": item.get("hostuin") or 0,
                    "fid": item.get("fid") or "",
                    "appid": item.get("appid") or 311,
                    "curkey": item.get("curkey") or "",
                },
            )
            return self._unwrap(resp) is not None
        except Exception as e:
            logger.warning(f"[{self.name}] 点赞失败: {e}")
            return False

    async def _comment(self, client: httpx.AsyncClient, item: dict, content: str) -> bool:
        try:
            resp = await client.post(
                "/comment",
                json={
                    "hostuin": item.get("hostuin") or 0,
                    "fid": item.get("fid") or "",
                    "appid": item.get("appid") or 311,
                    "content": content,
                    "busi_param": item.get("busi_param") or {},
                },
            )
            return self._unwrap(resp) is not None
        except Exception as e:
            logger.warning(f"[{self.name}] 评论失败: {e}")
            return False

    async def _compose(self, items: list[dict]) -> str:
        provider = None
        try:
            provider = self.p.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return ""
        nl = chr(10)
        lines = []
        for item in items[:4]:
            summary = str(item.get("summary") or "").strip()
            if summary:
                lines.append("- " + summary[:120].replace(chr(10), " "))
        if not lines:
            return ""
        prompt = (
            "刚翻到的几条动态：" + nl + nl.join(lines) + nl + nl
            + "写一条你自己的说说。没有想说的就回 SKIP。"
        )
        system_prompt = str(self._cfg("qzone_post_prompt", "") or "").strip() or POST_PROMPT
        try:
            resp = await provider.text_chat(
                prompt=prompt, system_prompt=system_prompt, contexts=[]
            )
        except Exception as e:
            logger.error(f"[{self.name}] 说起说说不成: {e}")
            return ""
        text = (getattr(resp, "completion_text", "") or "").strip().strip("\"“”'")
        if not text or text.upper().startswith("SKIP") or text.startswith("跳过"):
            return ""
        limit = int(self._cfg("qzone_post_max_len", 80) or 80)
        return text.replace(chr(10), " ")[:limit].strip()

    async def _publish(self, client: httpx.AsyncClient, content: str) -> bool:
        try:
            resp = await client.post("/post", json={"content": content})
            return self._unwrap(resp) is not None
        except Exception as e:
            logger.warning(f"[{self.name}] 发说说失败: {e}")
            return False

    def _post_allowed(self, st: dict) -> bool:
        if not self._cfg("qzone_post_enabled", True):
            return False
        if int(st.get("post_count", 0)) >= max(0, int(self._cfg("qzone_post_daily_max", 1) or 1)):
            return False
        prob = min(1.0, max(0.0, self._cfg("qzone_post_probability", 0.5)))
        return random.random() <= prob

    # ------------------------------------------------------------ 主流程

    async def run(self, force: bool = False, dry: bool = False) -> str:
        if not force and not self._cfg("qzone_enabled", False):
            return ""
        st = self._qstate()
        self._today_reset(st)
        daily = int(self._cfg("qzone_daily_max", 2) or 2)
        if not force and st.get("count", 0) >= daily:
            return ""

        limit = max(1, int(self._cfg("qzone_limit", 5) or 5))
        scope = str(self._cfg("qzone_scope", "active") or "active")
        max_age = float(self._cfg("qzone_max_age_hours", 72) or 72) * 3600

        client = await self._client()
        if client is None:
            logger.warning(f"[{self.name}] 没找到 qzone 守护进程，跳过")
            return ""
        report: list[str] = []
        try:
            async with client:
                resp = await client.get(
                    "/feeds", params={"hostuin": 0, "limit": limit, "scope": scope}
                )
                data = self._unwrap(resp) or {}
                items = data.get("items") or []
                me = int(data.get("hostuin") or 0)
                seen = set(st.get("seen") or [])
                now = time.time()
                for item in items:
                    fid = str(item.get("fid") or "")
                    if not fid or fid in seen:
                        continue
                    if me and int(item.get("hostuin") or 0) == me:
                        seen.add(fid)
                        continue
                    created = float(item.get("created_at") or 0)
                    if created and now - created > max_age:
                        seen.add(fid)
                        continue
                    if not force and st.get("count", 0) >= daily:
                        break

                    line = await self._judge(item, client)
                    seen.add(fid)
                    if not line:
                        continue
                    who = item.get("nickname") or "某人"
                    if dry:
                        report.append(f"（预演）{who}：{line}")
                        continue
                    if self._cfg("qzone_like", True):
                        await self._like(client, item)
                    if await self._comment(client, item, line):
                        st["count"] = int(st.get("count", 0)) + 1
                        report.append(f"{who}：{line}")
                        logger.info(f"[{self.name}] 在空间回了 {who}：{line}")
                if items and self._post_allowed(st):
                    text = await self._compose(items)
                    if text:
                        if dry:
                            report.append("（预演）说说：" + text)
                        elif await self._publish(client, text):
                            st["post_count"] = int(st.get("post_count", 0)) + 1
                            report.append("说说：" + text)
                            logger.info(f"[{self.name}] 发了条说说：{text}")
                st["seen"] = list(seen)[-200:]
                st["last_check"] = time.time()
        except Exception as e:
            logger.error(f"[{self.name}] 空间巡检失败: {e}")
        finally:
            self.p._save_state()
        return chr(10).join(report)
