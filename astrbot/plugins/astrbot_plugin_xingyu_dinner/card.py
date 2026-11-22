"""投喂卡渲染：把 DeepSeek 充值链接做成一张现画现发的小卡片。

每次调用都是全新绘制（时间、卡号、饭钱、纸纹都变），不缓存图片。
"""

from __future__ import annotations

import os
import random
import time
from functools import lru_cache

import qrcode
from PIL import Image, ImageDraw, ImageFont

W, H = 720, 1206
MARGIN = 56

PAPER = (247, 243, 235)
INK = (26, 24, 21)
MUTED = (124, 116, 103)
HAIR = (215, 207, 193)
NAVY = (38, 51, 92)
WHITE = (255, 255, 255)

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

FONT_CANDIDATES = {
    "bold": ["msyhbd.ttc", "simhei.ttf", "Deng.ttf"],
    "reg": ["msyh.ttc", "Deng.ttf", "simhei.ttf"],
    "mono": ["consola.ttf", "cour.ttf", "msyh.ttc"],
}


@lru_cache(maxsize=64)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for name in FONT_CANDIDATES.get(kind, FONT_CANDIDATES["reg"]):
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _caps(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int,
    color: tuple[int, int, int],
    tracking: int = 3,
) -> int:
    """给拉丁小标题加字距，返回结束时的 x。"""
    x, y = xy
    f = font("mono", size)
    for ch in text:
        draw.text((x, y), ch, font=f, fill=color)
        x += draw.textlength(ch, font=f) + tracking
    return int(x)


def _hairline(draw: ImageDraw.ImageDraw, y: int, color=HAIR) -> None:
    draw.line([(MARGIN, y), (W - MARGIN, y)], fill=color, width=1)


def _paper_texture(img: Image.Image, seed: int) -> None:
    """一点点纸纹噪点，让每张卡都不一样。"""
    rnd = random.Random(seed)
    noise = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(noise)
    for _ in range(2600):
        x = rnd.randrange(12, W - 12)
        y = rnd.randrange(12, H - 12)
        a = rnd.choice((6, 8, 10, 12))
        d.point((x, y), fill=(90, 82, 68, a))
    img.alpha_composite(noise)


def _qr_image(url: str, box_size: int = 10) -> Image.Image:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(fill_color=INK, back_color=WHITE).convert("RGB")


def render_card(
    out_path: str,
    url: str,
    *,
    amount: float,
    title: str = "<BOT_NAME>吃饭啦",
    subtitle: str = "今日菜单 · DeepSeek 充值通道",
    footer: str = "笔杆是魔杖，文字是咒语。",
    channel_label: str = "platform.deepseek.com/top_up",
    qr_image_path: str | None = None,
    qr_caption: str = "扫码投喂 · 支付宝 / 微信支付",
    qr_hint: str = "扫码后在 DeepSeek 官网完成充值即可",
    notice: str = "",
    now: float | None = None,
) -> str:
    ts = time.time() if now is None else now
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    card_id = f"093-{time.strftime('%y%m%d', time.localtime(ts))}-{random.randint(1000, 9999)}"

    img = Image.new("RGBA", (W, H), PAPER + (255,))
    _paper_texture(img, seed=int(ts) ^ random.getrandbits(16))
    draw = ImageDraw.Draw(img)

    # 顶部：小标题 + DS 印章
    _caps(draw, (MARGIN, 56), "TOKEN TOP-UP", 18, MUTED, tracking=4)
    chip_w, chip_h = 74, 46
    chip_x, chip_y = W - MARGIN - chip_w, 50
    draw.rounded_rectangle(
        [chip_x, chip_y, chip_x + chip_w, chip_y + chip_h], radius=12, fill=NAVY
    )
    draw.text(
        (chip_x + chip_w / 2, chip_y + chip_h / 2 - 1),
        "DS",
        font=font("bold", 24),
        fill=WHITE,
        anchor="mm",
    )
    draw.text(
        (W - MARGIN, chip_y + chip_h + 9),
        card_id,
        font=font("mono", 17),
        fill=MUTED,
        anchor="ra",
    )

    # 主标题
    draw.text((MARGIN, 104), title, font=font("bold", 66), fill=INK)
    draw.text((MARGIN + 3, 196), subtitle, font=font("reg", 26), fill=MUTED)
    _hairline(draw, 254)

    # 二维码白卡
    card_top, card_h = 292, 500
    draw.rounded_rectangle(
        [MARGIN, card_top, W - MARGIN, card_top + card_h], radius=18, fill=WHITE
    )
    qr_box = 372
    if qr_image_path and os.path.exists(qr_image_path):
        # 用自己的收款码图：等比塞进白卡中央
        own = Image.open(qr_image_path).convert("RGB")
        own.thumbnail((qr_box, qr_box), Image.LANCZOS)
        qr_img = Image.new("RGB", (qr_box, qr_box), WHITE)
        qr_img.paste(own, ((qr_box - own.width) // 2, (qr_box - own.height) // 2))
    else:
        qr_img = _qr_image(url).resize((qr_box, qr_box), Image.NEAREST)
    qx = (W - qr_box) // 2
    qy = card_top + 30
    img.paste(qr_img, (qx, qy))

    draw.text(
        (W / 2, qy + qr_box + 34),
        qr_caption,
        font=font("reg", 25),
        fill=INK,
        anchor="mm",
    )
    draw.text(
        (W / 2, qy + qr_box + 68),
        qr_hint,
        font=font("reg", 19),
        fill=MUTED,
        anchor="mm",
    )

    # 信息行
    rows = [
        ("饭钱", f"¥ {amount:.2f}"),
        ("生成于", stamp),
        ("通道", channel_label),
    ]
    y = 838
    for k, v in rows:
        draw.text((MARGIN, y), k, font=font("reg", 21), fill=MUTED)
        draw.text((W - MARGIN, y - 3), v, font=font("mono", 24), fill=INK, anchor="ra")
        y += 52

    # 风险提示
    if notice:
        parts = notice.splitlines() or []
        lines = [y for y in (x.strip() for chunk in parts for x in chunk.split("；")) if y]
        box_top = 996
        box_h = 26 + 30 * len(lines) + 8
        draw.rectangle(
            [MARGIN, box_top, W - MARGIN, box_top + box_h],
            fill=(252, 249, 242),
            outline=(206, 196, 180),
            width=1,
        )
        ty = box_top + 20
        for i, line in enumerate(lines):
            draw.text((MARGIN + 20, ty), ("！" if i == 0 else "·") + " " + line,
                      font=font("reg", 19), fill=(120, 62, 38) if i == 0 else MUTED)
            ty += 30
        footer_y = box_top + box_h + 26
    else:
        footer_y = 1018

    _hairline(draw, footer_y - 12)

    # 页脚
    draw.text((MARGIN, footer_y), footer, font=font("reg", 21), fill=MUTED)
    draw.text(
        (W - MARGIN, footer_y),
        "<BOT_NAME>",
        font=font("mono", 20),
        fill=NAVY,
        anchor="ra",
    )
    draw.text(
        (MARGIN, footer_y + 34),
        "版本号停在 093 就好啦 · 本卡每次现画，不作报销凭证",
        font=font("reg", 17),
        fill=(160, 152, 138),
    )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path
