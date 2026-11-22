"""看头像：把 QQ 头像抓下来，用一个小视觉模型看懂它，回一句话。"""

import asyncio
import os
import tempfile
import urllib.request

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Image
from astrbot.core.platform import AstrMessageEvent

MODEL_DIR = r"C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378\data\models\blip"


class AvatarPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._proc = None
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from transformers import BlipForConditionalGeneration, BlipProcessor

            self._proc = BlipProcessor.from_pretrained(MODEL_DIR)
            self._model = BlipForConditionalGeneration.from_pretrained(MODEL_DIR)
        return self._proc, self._model

    def _caption(self, path: str) -> str:
        from PIL import Image

        proc, model = self._ensure_model()
        img = Image.open(path).convert("RGB")
        out = model.generate(**proc(img, return_tensors="pt"), max_new_tokens=30)
        return proc.decode(out[0], skip_special_tokens=True)

    @filter.llm_tool()
    async def see_avatar(
        self,
        event: AstrMessageEvent,
        user_id: str = "",
        show_image: bool = False,
    ):
        """看一眼某人的 QQ 头像，返回画面内容的英文描述（你可以自己译成中文说出来）。

        当用户问"你看得见我头像吗""我头像是啥""换个头像了看见没"时调用。

        Args:
            user_id(string): 要看的 QQ 号，留空则看当前说话人的头像
            show_image(boolean): 是否把头像图片也发出来，默认不发
        """
        uid = (user_id or "").strip() or str(event.get_sender_id())
        if not uid.isdigit():
            return "这个号看着不像 QQ 号。"

        url = f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
        path = os.path.join(tempfile.mkdtemp(prefix="avatar_"), f"{uid}.jpg")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r, open(path, "wb") as f:
                f.write(r.read())
        except Exception as e:  # noqa: BLE001
            logger.error(f"[avatar] 下载头像失败: {e}")
            return "头像没拉下来，可能网络不太行。"

        if os.path.getsize(path) < 1024:
            return "这人好像没设头像。"

        try:
            cap = await asyncio.get_running_loop().run_in_executor(
                None, self._caption, path
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[avatar] 描述头像失败: {e}")
            cap = "看不清画面"

        if show_image:
            try:
                await event.send(event.chain_result([Image.fromFileSystem(path)]))
            except Exception:  # noqa: BLE001
                pass

        return f"{uid} 的头像画面：{cap}"

    async def terminate(self):
        self._model = None
        self._proc = None
