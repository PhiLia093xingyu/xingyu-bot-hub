"""本地画图：把 data/models/sd15 跑成常驻 worker，给 agent 一个 local_draw 工具。

模型加载和推理都在独立子进程里，不占 AstrBot 主进程的显存/内存，也不会阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

DEFAULTS = {
    "model_path": "",
    "torch_path": r"D:\astrbot-cuda",
    "device": "auto",
    "style_prefix": "anime illustration, ",
    "steps": 24,
    "size": 512,
    "guidance": 7.5,
    "negative": (
        "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
        "fewer digits, cropped, worst quality, low quality, jpeg artifacts, signature, "
        "watermark, username, blurry, ai-generated"
    ),
    "timeout": 240,
    "load_timeout": 300,
    "max_size": 768,
    "cooldown": 10,
}


@register(
    "astrbot_plugin_local_sd",
    "<BOT_NAME>",
    "本地 SD1.5 画图：local_draw 工具 + /画画 命令，GPU 常驻 worker",
    "v0.1.0",
    "",
)
class LocalSDPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_local_sd")
        self.out_dir = self.data_dir / "images"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._info: dict = {}
        self._last_call: dict[str, float] = {}

    # ------------------------------------------------------------ config
    def _cfg(self, key: str):
        value = None
        if hasattr(self.config, "get"):
            value = self.config.get(key)
        return DEFAULTS[key] if value is None else value

    def _model_path(self) -> str:
        configured = str(self._cfg("model_path") or "").strip()
        if configured:
            return configured
        return os.path.join(get_astrbot_data_path(), "models", "sd15")

    # ------------------------------------------------------------ worker
    async def _ensure_worker(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None and self._info:
            return self._proc

        here = os.path.dirname(os.path.abspath(__file__))
        worker = os.path.join(here, "sd_worker.py")
        env = os.environ.copy()
        env["SD_MODEL_PATH"] = self._model_path()
        env["SD_TORCH_PATH"] = str(self._cfg("torch_path") or "")
        env["SD_DEVICE"] = str(self._cfg("device") or "auto")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        import sys

        logger.info("[local_sd] 启动 worker，模型 %s", self._model_path())
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            worker,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=here,
            env=env,
        )
        line = await asyncio.wait_for(
            self._proc.stdout.readline(), timeout=float(self._cfg("load_timeout"))
        )
        if not line:
            err = await self._proc.stderr.read()
            raise RuntimeError(f"worker 启动失败: {err.decode('utf-8', 'ignore')[-500:]}")
        info = json.loads(line.decode("utf-8"))
        if not info.get("ok"):
            raise RuntimeError(f"worker 加载失败: {info.get('error')}")
        self._info = info
        logger.info(
            "[local_sd] 模型就绪 device=%s gpu=%s 耗时 %ss",
            info.get("device"), info.get("gpu"), info.get("load_seconds"),
        )
        return self._proc

    async def _request(self, payload: dict, timeout: float) -> dict:
        proc = await self._ensure_worker()
        assert proc.stdin and proc.stdout
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError("worker 没有响应（可能崩了）")
        return json.loads(line.decode("utf-8"))

    async def _draw(self, prompt: str, negative: str, steps: int, size: int) -> dict:
        prefix = str(self._cfg("style_prefix") or "").strip()
        if prefix and prefix.lower() not in prompt.lower():
            prompt = f"{prefix}{prompt}"
        out = os.path.join(str(self.out_dir), f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.png")
        payload = {
            "prompt": prompt,
            "negative": negative,
            "steps": steps,
            "width": size,
            "height": size,
            "out": out,
            "guidance": float(self._cfg("guidance")),
        }
        async with self._lock:  # 串行，别把 6G 显存干爆
            return await self._request(payload, timeout=float(self._cfg("timeout")))

    def _cooldown_left(self, key: str) -> float:
        try:
            cooldown = float(self._cfg("cooldown"))
        except (TypeError, ValueError):
            cooldown = 10.0
        left = self._last_call.get(key, 0.0) + cooldown - time.time()
        return left if left > 0 else 0.0

    def _cleanup_images(self, keep: int = 100) -> None:
        """只留最近 keep 张，别把 C 盘塞满。"""
        try:
            files = sorted(
                (p for p in self.out_dir.iterdir() if p.suffix.lower() == ".png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in files[keep:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------ prompt
    @filter.on_llm_request()
    async def inject_draw_tool_hint(self, event: AstrMessageEvent, req) -> None:
        """钉一条规则：要画图就用 local_draw。"""
        hint = (
            "【工具规则】用户要「画一张」「生成一张图」「来个插画」时，必须调用 local_draw 工具"
            "（本机显卡上的 SD1.5，一次约 20 秒），不要自己写脚本调模型。"
        )
        current = getattr(req, "system_prompt", "") or ""
        if "local_draw 工具" in current:
            return
        req.system_prompt = current + "\n\n" + hint

    # ------------------------------------------------------------ tool
    @filter.llm_tool(name="local_draw")
    async def local_draw(
        self,
        event: AstrMessageEvent,
        prompt: str,
        negative: str = "",
        steps: int = 0,
        size: int = 0,
    ):
        """用本地显卡上的 Stable Diffusion 1.5 画一张二次元插画，并直接把图发到当前会话。

        当用户要"画一张""帮我生成一张图"，或者要某个角色/场景的原创图时调用。
        画图需要十几秒，画完图片会自动发出，你只需要补一句短评。

        Args:
            prompt(string): 画面描述，英文效果最好，例如 "1girl, cat ears, orange hair, pajamas, soft lighting, masterpiece"
            negative(string): 不希望出现的内容，留空用默认的负面词
            steps(number): 采样步数，0 表示用默认值（24），越多越慢越精细
            size(number): 图片边长，0 表示用默认值（512），最大 768
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return json.dumps({"ok": False, "error": "prompt 是空的"}, ensure_ascii=False)

        session_key = getattr(event, "unified_msg_origin", "") or "global"
        wait = self._cooldown_left(session_key)
        if wait > 0:
            return json.dumps({"ok": False, "error": f"刚画过，{wait:.0f} 秒后再来"}, ensure_ascii=False)

        try:
            default_steps = int(self._cfg("steps"))
            max_size = int(self._cfg("max_size"))
        except (TypeError, ValueError):
            default_steps, max_size = 24, 768
        use_steps = int(steps) if steps else default_steps
        use_steps = max(8, min(use_steps, 50))
        use_size = int(size) if size else int(self._cfg("size") or 512)
        use_size = max(256, min(use_size, max_size, 768))
        neg = negative.strip() or str(self._cfg("negative") or "")

        self._last_call[session_key] = time.time()
        try:
            result = await self._draw(prompt, neg, use_steps, use_size)
        except asyncio.TimeoutError:
            return json.dumps({"ok": False, "error": "画图超时了"}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[local_sd] 画图失败: {exc}")
            return json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False)

        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "未知错误")[:300]}, ensure_ascii=False)

        path = result["path"]
        self._cleanup_images()
        try:
            await event.send(event.chain_result([Image.fromFileSystem(path)]))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[local_sd] 发送失败: {exc}")
            return json.dumps({"ok": False, "error": f"画好了但没发出去: {exc}"}, ensure_ascii=False)

        return json.dumps(
            {
                "ok": True,
                "prompt": prompt,
                "size": result.get("size"),
                "steps": result.get("steps"),
                "seconds": result.get("seconds"),
                "device": self._info.get("gpu") or self._info.get("device"),
                "note": "图已经发出去了，补一句短评即可。",
            },
            ensure_ascii=False,
        )

    # ------------------------------------------------------------ command
    @filter.command("画画")
    async def cmd_draw(self, event: AstrMessageEvent, prompt: str = ""):
        """手动画图：/画画 1girl, cat ears, orange hair"""
        prompt = (prompt or "").strip() or (event.message_str or "").replace("/画画", "").strip()
        if not prompt:
            yield event.plain_result("用法：/画画 1girl, cat ears, orange hair（英文提示词效果最好）")
            return

        session_key = getattr(event, "unified_msg_origin", "") or event.get_sender_id()
        wait = self._cooldown_left(session_key)
        if wait > 0:
            yield event.plain_result(f"刚画过，{wait:.0f} 秒后再试。")
            return

        self._last_call[session_key] = time.time()
        yield event.plain_result("画着呢，大概十几秒……")
        try:
            result = await self._draw(
                prompt,
                str(self._cfg("negative") or ""),
                int(self._cfg("steps")),
                int(self._cfg("size")),
            )
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"画失败了：{exc}")
            return
        if not result.get("ok"):
            yield event.plain_result(f"画失败了：{result.get('error', '未知错误')[:200]}")
            return
        yield event.chain_result([Image.fromFileSystem(result["path"])])

    @filter.command("画图状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """看 worker 状态：/画图状态"""
        lines = [f"模型：{self._model_path()}"]
        if self._info:
            lines.append(
                f"worker 已就绪：device={self._info.get('device')} gpu={self._info.get('gpu')} "
                f"torch={self._info.get('torch')} 加载耗时={self._info.get('load_seconds')}s"
            )
        else:
            lines.append("worker 还没启动（第一次画图时会加载模型，约 15~30 秒）")
        lines.append(f"默认 {self._cfg('steps')} 步 / {self._cfg('size')}px，torch 路径 {self._cfg('torch_path')}")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        proc = self._proc
        self._proc = None
        self._info = {}
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.write(b'{"cmd":"quit"}\n')
                    await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
