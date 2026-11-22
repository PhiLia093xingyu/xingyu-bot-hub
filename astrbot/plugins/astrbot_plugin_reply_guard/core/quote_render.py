"""引用图渲染。

布局、配色、尺寸对照 zjutjh/qq-quote-generator（Go + SVG 版）重写的 Python 实现：
卡片 600 宽、圆角 12、浅色卡片底、头像 40 圆形、昵称 12、气泡白色圆角带内边距、
正文 15/行高 24、嵌套引用 2px 竖条 + 8px 缩进。渲染倍率 2 倍输出。
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, Sequence

from astrbot.api import logger

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFont = None


# ------------------------------------------------------------------ 常量

OUTPUT_SCALE = 2.0
# QQ 会把动图显示得比静态图小一圈，导出时先放大，抵消它的缩放
GIF_EXPORT_WIDTH = 1200  # 不缩水，宁可文件大一点（约 6MB 以内）  # QQ 对动图固定显示宽度，导出的画布宽度定在这附近最清楚
GIF_MAX_PIXELS = 40_000_000

CARD_MAX_WIDTH = 600.0
CARD_PAD_X = 12.0
CARD_PAD_Y = 16.0
AVATAR_SIZE = 40.0
ROW_GAP = 10.0
ROW_MARGIN = 14.0
NICKNAME_SIZE = 12.0
NICKNAME_HEIGHT = 16.0
NICKNAME_MARGIN = 4.0
BUBBLE_PAD_X = 12.0
BUBBLE_PAD_Y = 8.0
TEXT_SIZE = 15.0
TEXT_LINE_HEIGHT = 24.0
SEGMENT_MARGIN = 4.0
VOICE_MIN_WIDTH = 72.0
VOICE_MAX_WIDTH = 168.0
VOICE_BAR_GAP = 2.5
VOICE_BAR_WIDTH = 2.0
VOICE_TRANS_SIZE = 13.0
VOICE_TRANS_LINE = 18.0
INLINE_EMOJI_SIZE = 24.0
INLINE_EMOJI_GAP = 2.0
IMAGE_RADIUS = 6.0
BUBBLE_RADIUS = 12.0
# 卡片顶部的群名 / 群头像
GROUP_AVATAR_SIZE = 34.0
GROUP_NAME_SIZE = 17.0
GROUP_NAME_HEIGHT = 24.0
GROUP_HEADER_GAP = 10.0
GROUP_HEADER_MARGIN = 14.0
# 卡片顶部的时间行
TIME_FONT_SIZE = 11.0
TIME_HEIGHT = 16.0
TIME_MARGIN = 6.0
# 两条消息隔得比这久，就再标一次时间（秒）
TIME_GAP_SECONDS = 20 * 60
REPLY_BAR_WIDTH = 2.0
REPLY_INDENT = 8.0
REPLY_BODY_MARGIN = 8.0

# 昵称后面的小徽章：群头衔、群等级
BADGE_HEIGHT = 15.0
BADGE_PAD_X = 5.0
BADGE_GAP = 4.0
BADGE_MARGIN = 6.0
BADGE_FONT_SIZE = 10.0
BADGE_BG = "#eef1f6"
BADGE_COLOR = "#667085"
# 头像挂件比头像本身大一圈，居中叠上去
PENDANT_SCALE = 1.5

# 昵称后面的互动标识图标（龙王 / 之火 / 炽焰）
HONOR_SIZE = 18.0
HONOR_GAP = 3.0
HONOR_MARGIN = 5.0
HONOR_FONT_SIZE = 9.5
HONOR_STYLES = {
    "龙王": ("龙", "#e0a33a"),
    "之火": ("火", "#e8792d"),
    "炽焰": ("焰", "#d24b3a"),
}

# 表情回应：气泡底部的小胶囊
REACTION_HEIGHT = 17.0
REACTION_PAD_X = 3.5
REACTION_GAP = 3.0
REACTION_EMOJI_SIZE = 13.0
REACTION_EMOJI_GAP = 2.5
REACTION_FONT_SIZE = 10.0
REACTION_MARGIN = 4.0
REACTION_OUTER_GAP = 5.0  # 表情回应与气泡之间的间距
REACTION_BG = "#f1f2f5"
REACTION_COLOR = "#6b7280"

CARD_BG = "#f7f8fb"
AVATAR_BG = "#d9dee8"
NICKNAME_COLOR = "#667085"
BUBBLE_BG = "#ffffff"
MESSAGE_COLOR = "#242937"
REPLY_TEXT_COLOR = "#475467"
REPLY_BAR_COLOR = "#d0d5dd"


@dataclass
class CardTheme:
    """卡片配色，一项都不填就是默认的浅色样式。"""

    card_bg: str = CARD_BG
    bubble_bg: str = BUBBLE_BG
    nickname_color: str = NICKNAME_COLOR
    message_color: str = MESSAGE_COLOR
    reply_text_color: str = REPLY_TEXT_COLOR
    reply_bar_color: str = REPLY_BAR_COLOR
    badge_bg: str = BADGE_BG
    badge_color: str = BADGE_COLOR
    reaction_bg: str = REACTION_BG
    reaction_color: str = REACTION_COLOR
    emoji_style: str = "apple"  # apple / font / google / qq / twemoji
    reaction_style: str = "chip"  # chip=带底色的小胶囊，plain=只留表情和数字
    tail: bool = True  # 气泡左侧那个小尖角

    def with_overrides(self, **kwargs: Any) -> "CardTheme":
        data = {field: getattr(self, field) for field in self.__dataclass_fields__}
        for key, value in kwargs.items():
            if key in data and value not in (None, ""):
                data[key] = value
        return CardTheme(**data)


DEFAULT_THEME = CardTheme()


# 群标题与消息之间的分隔线
DIVIDER_COLOR = "#e9ecf1"

MAX_REPLY_DEPTH = 3
MAX_REPLY_NODES = 30
REPLY_UNAVAILABLE_TEXT = "[引用消息不可用]"
REPLY_OMITTED_TEXT = "[更早的回复已省略]"

MAX_GIF_FRAMES = 100
MAX_GIF_DURATION = 500  # 厘秒
MIN_FRAME_DELAY = 2

AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"
QFACE_BASE_URL = "https://koishi.js.org/QFace/assets/qq_emoji"
MAX_IMAGE_BYTES = 16 << 20

# 取图时带的头。QQ 的图片直链有防盗链，缺 Referer 时经常直接 403。
IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://q.qq.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

_REMOTE_PREFIXES = ("http://", "https://")

_FONT_HINTS = (
    "simhei",
    "notosanscjk",
    "notosanssc",
    "notosans",
    "sourcehansans",
    "sourcehans",
    "wqy",
    "msyh",
    "droidsansfallback",
    "cjk",
)
_FONT_ROOTS = (
    "/AstrBot/data/fonts",
    "/AstrBot/data/koko/fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "C:/Windows/Fonts",
)

EMOJI_FONT_SIZE = 109  # Noto Color Emoji 是位图字体，只有这个字号
EMOJI_SCALE = 1.2

_EMOJI_FONT_ROOTS = (
    "/AstrBot/data/plugin_data/astrbot_plugin_reply_guard/fonts",
    "/AstrBot/data/fonts",
    "/AstrBot/data/koko/fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "C:/Windows/Fonts",
)
_EMOJI_FONT_HINTS = ("notocoloremoji", "applecoloremoji", "seguiemj", "emoji")

# 主字体缺字（比如昵称里的 ꧁༺ོ࿈༻꧂ 这类装饰符）时的回退链：
# 符号字体 → 藏文 / 天城文 → CJK 扩展 → 兜底中文字体
_FALLBACK_FONT_HINTS = (
    "seguisym",
    "seguihis",
    "segoeui",
    "himalaya",
    "nirmala",
    "arialuni",
    "msyi",
    "micross",
    "simsunb",
    "mingliub",
    "simsun",
    "simhei",
    "notosanssymbols",
    "noto",
)
_FALLBACK_SKIP = ("emj", "emoji")  # 彩色位图字体只能在专用尺寸下用，不进回退链

_EMOJI_CHAR_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U0001F3FB-\U0001F3FF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\u2190-\u21FF"
    "]"
)

_font_cache: dict[tuple[str, int], Any] = {}
_resolved_font: str | None = None
_resolved_emoji_font: str | None = None
_emoji_font_obj: Any = None


def resolve_font_path(preferred: str = "") -> str:
    global _resolved_font
    if preferred:
        path = os.path.expanduser(preferred)
        if os.path.isfile(path):
            return path
    if _resolved_font is not None:
        return _resolved_font

    found: list[str] = []
    for root in _FONT_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                lower = name.lower()
                if lower.endswith((".ttf", ".otf", ".ttc")):
                    found.append(os.path.join(dirpath, name))
    for path in found:
        if any(hint in os.path.basename(path).lower() for hint in _FONT_HINTS):
            _resolved_font = path
            return path
    for path in found:
        lower = os.path.basename(path).lower()
        if "dejavu" in lower or "emoji" in lower:
            continue
        _resolved_font = path
        return path
    _resolved_font = ""
    return ""


def _load_font(font_path: str, size: float):
    px = max(1, int(round(size * OUTPUT_SCALE)))
    key = (font_path, px)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    if not font_path:
        font = ImageFont.load_default()
    else:
        try:
            font = ImageFont.truetype(font_path, px)
        except Exception:
            try:
                font = ImageFont.truetype(font_path, px, index=0)
            except Exception:
                font = ImageFont.load_default()
    _font_cache[key] = font
    return font


_cmap_cache: dict[str, set[int] | None] = {}
_fallback_fonts: list[str] | None = None


def _load_cmap(path: str) -> set[int] | None:
    """读字体的 cmap，用来判断某个字符有没有字形。没有 fontTools 就返回 None。"""
    if path in _cmap_cache:
        return _cmap_cache[path]
    cmap: set[int] | None = None
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(path, fontNumber=0, lazy=True)
        try:
            cmap = set(font.getBestCmap() or ())
        finally:
            font.close()
    except Exception as e:
        logger.debug(f"[reply_guard] 读 cmap 失败 {path}: {e}")
        cmap = None
    _cmap_cache[path] = cmap
    return cmap


def _font_covers(path: str, char: str) -> bool:
    """这个字体里有没有这个字的字形。查不到 cmap 时按「有」处理，免得误伤。"""
    if not path:
        return False
    cmap = _load_cmap(path)
    if cmap is None:
        return True
    return ord(char) in cmap


def _plugin_font_dirs() -> list[str]:
    """插件自己的字体目录（data/plugin_data/<插件名>/fonts），用户丢字体进去就能用。"""
    here = os.path.dirname(os.path.abspath(__file__))
    plugin_root = os.path.dirname(here)
    dirs = [os.path.join(plugin_root, "fonts")]
    try:
        data_root = os.path.dirname(os.path.dirname(plugin_root))  # data/plugins/xx -> data
        dirs.insert(0, os.path.join(data_root, "plugin_data", os.path.basename(plugin_root), "fonts"))
    except Exception:
        pass
    return [d for d in dirs if os.path.isdir(d)]


def _scan_font_files(roots: Sequence[str]) -> list[str]:
    found: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.lower().endswith((".ttf", ".otf", ".ttc")):
                    found.append(os.path.join(dirpath, name))
    return found


def _resolve_fallback_fonts() -> list[str]:
    """按 _FALLBACK_FONT_HINTS 的优先级收集可用的回退字体，插件自带的排最前。"""
    global _fallback_fonts
    if _fallback_fonts is not None:
        return _fallback_fonts

    owned = _scan_font_files(_plugin_font_dirs())
    found = _scan_font_files(_FONT_ROOTS)

    ordered: list[str] = [p for p in owned if not any(s in os.path.basename(p).lower() for s in _FALLBACK_SKIP)]
    for hint in _FALLBACK_FONT_HINTS:
        for path in found:
            lower = os.path.basename(path).lower()
            if hint not in lower or path in ordered:
                continue
            if any(skip in lower for skip in _FALLBACK_SKIP):
                continue
            ordered.append(path)
    _fallback_fonts = ordered
    return ordered


def resolve_emoji_font_path(preferred: str = "") -> str:
    """找彩色 emoji 字体（Noto Color Emoji / Apple Color Emoji 那一类）。"""
    global _resolved_emoji_font
    if preferred:
        path = os.path.expanduser(preferred)
        if os.path.isfile(path):
            return path
    if _resolved_emoji_font is not None:
        return _resolved_emoji_font

    found: list[str] = []
    for root in _EMOJI_FONT_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.lower().endswith((".ttf", ".otf", ".ttc")):
                    found.append(os.path.join(dirpath, name))

    for path in found:
        if any(hint in os.path.basename(path).lower() for hint in _EMOJI_FONT_HINTS):
            _resolved_emoji_font = path
            return path

    _resolved_emoji_font = ""
    return ""


def _load_emoji_font():
    global _emoji_font_obj
    if _emoji_font_obj is not None:
        return _emoji_font_obj
    path = _resolved_emoji_font or ""
    if not path:
        return None
    try:
        _emoji_font_obj = ImageFont.truetype(path, EMOJI_FONT_SIZE)
    except Exception:
        try:
            _emoji_font_obj = ImageFont.truetype(path, EMOJI_FONT_SIZE, index=0)
        except Exception as e:
            logger.debug(f"[reply_guard] emoji 字体打不开: {e}")
            _emoji_font_obj = None
    return _emoji_font_obj


def _render_emoji(char: str, target_height: int):
    """用彩色字体画一个 emoji，再缩放到目标高度。"""
    font = _load_emoji_font()
    if font is None or not char:
        return None
    try:
        canvas = Image.new(
            "RGBA", (EMOJI_FONT_SIZE * 2, EMOJI_FONT_SIZE * 2), (0, 0, 0, 0)
        )
        ImageDraw.Draw(canvas).text((0, 0), char, font=font, embedded_color=True)
        box = canvas.getbbox()
        if not box:
            return None
        canvas = canvas.crop(box)
        ratio = target_height / max(1, canvas.height)
        return canvas.resize(
            (max(1, int(round(canvas.width * ratio))), max(1, target_height)),
            Image.LANCZOS,
        )
    except Exception as e:
        logger.debug(f"[reply_guard] emoji 渲染失败 {char!r}: {e}")
        return None


# ------------------------------------------------------------------ 数据结构


@dataclass
class MessageSegment:
    type: str = "text"  # text | image | face | record
    kind: str = ""  # emoji | sticker | voice
    text: str = ""
    url: str = ""
    id: str = ""
    transcript: str = ""  # 语音转文字，画在语音条下面、同一个气泡里


@dataclass
class ReplyMessage:
    nickname: str = ""
    segments: list[MessageSegment] = field(default_factory=list)
    reply: "ReplyMessage | None" = None
    message_id: str = ""


@dataclass
class QuoteMessage:
    user_id: str = ""
    nickname: str = ""
    segments: list[MessageSegment] = field(default_factory=list)
    reply: ReplyMessage | None = None
    message_id: str = ""
    time: int = 0
    avatar: str = ""
    reactions: list[dict] = field(default_factory=list)  # 表情回应
    honor: str = ""  # 群互动标识：龙王 / 之火 / 炽焰
    title: str = ""  # 群头衔
    level: str = ""  # 群等级
    pendant: str = ""  # 头像挂件图片地址


@dataclass
class LoadedImage:
    image: Any = None
    width: int = 0
    height: int = 0
    animation: list[tuple[Any, int]] | None = None


# ------------------------------------------------------------------ 测量 / 换行


_CN_PUNCT_MAP = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}


def _normalize_cn_punct(text: str) -> str:
    """语音转写常给半角标点，中文语境下换成全角。"""
    if not text or not re.search(r"[\u4e00-\u9fff]", text):
        return text
    text = re.sub(r"[,?!:;]", lambda m: _CN_PUNCT_MAP[m.group(0)], text)
    # 数字之间的小数点别动
    text = re.sub(r"(?<![0-9])\.(?!\d)", "。", text)
    text = text.replace("(", "（").replace(")", "）")
    text = text.replace("  ", " ")
    return text


class FontManager:
    def __init__(self, font_path: str, emoji_font_path: str = "") -> None:
        self.font_path = font_path
        self.emoji_font_path = emoji_font_path or resolve_emoji_font_path()
        self.fallback_paths = _resolve_fallback_fonts()
        self._char_font: dict[str, str] = {}
        self._char_style_cache: dict[str, tuple[bool, str]] = {}

    def font(self, size: float):
        return _load_font(self.font_path, size)

    def find_text_font(self, char: str) -> str:
        """找一个有这个字单色字形的文本字体，找不到返回空串。"""
        cached = self._char_font.get(char)
        if cached is not None:
            return cached
        chosen = ""
        if _font_covers(self.font_path, char):
            chosen = self.font_path
        else:
            for path in self.fallback_paths:
                if _font_covers(path, char):
                    chosen = path
                    break
        self._char_font[char] = chosen
        return chosen

    def font_for(self, char: str) -> str:
        """给单个字符挑字体：主字体有字形就用主字体，否则顺着回退链找。"""
        return self.find_text_font(char) or self.font_path

    @property
    def has_emoji_font(self) -> bool:
        return bool(self.emoji_font_path) and _load_emoji_font() is not None

    def _char_style(self, char: str) -> tuple[bool, str]:
        """决定一个字符走单色文本还是彩色 emoji。

        顺序：主字体有字形就画单色 → 像 emoji 的字符交给彩色 emoji 字体 →
        再顺着回退链找（藏文、爪哇文、CJK 扩展这些）。这样 ★ ☆ → 用中文字体
        的单色字形，不会出现彩色字体画出来的白框和残笔。
        """
        cached = self._char_style_cache.get(char)
        if cached is not None:
            return cached

        if _font_covers(self.font_path, char):
            style = (False, self.font_path)
        elif (
            _EMOJI_CHAR_RE.match(char)
            and self.has_emoji_font
            and _font_covers(self.emoji_font_path, char)
        ):
            style = (True, self.emoji_font_path)
        else:
            style = (False, self.font_path)
            for path in self.fallback_paths:
                if _font_covers(path, char):
                    style = (False, path)
                    break
        self._char_style_cache[char] = style
        return style

    @staticmethod
    def runs(text: str) -> list[tuple[bool, str]]:
        """按 emoji / 非 emoji 切段，方便分段换字体。"""
        runs: list[tuple[bool, str]] = []
        for char in text:
            is_emoji = bool(_EMOJI_CHAR_RE.match(char))
            if runs and runs[-1][0] == is_emoji:
                runs[-1] = (is_emoji, runs[-1][1] + char)
            else:
                runs.append((is_emoji, char))
        return runs

    def styled_runs(self, text: str) -> list[tuple[bool, str, str]]:
        """按 (是否彩色 emoji, 该用哪个字体, 片段) 切段，测量和绘制共用同一套切法。"""
        runs: list[tuple[bool, str, str]] = []
        for char in text or "":
            is_emoji, path = self._char_style(char)
            if runs and runs[-1][0] == is_emoji and runs[-1][1] == path:
                runs[-1] = (is_emoji, path, runs[-1][2] + char)
            else:
                runs.append((is_emoji, path, char))
        return runs

    def _chunk_width(self, path: str, chunk: str, size: float) -> float:
        font = _load_font(path or self.font_path, size)
        try:
            return float(font.getlength(chunk)) / OUTPUT_SCALE
        except Exception:
            return len(chunk) * size * 0.6

    def _text_measure(self, text: str, size: float, emoji_scale: float) -> float:
        total = 0.0
        for is_emoji, path, chunk in self.styled_runs(text):
            if is_emoji and emoji_scale != 1.0:
                total += size * emoji_scale * len(chunk)
                continue
            total += self._chunk_width(path, chunk, size)
        return total

    def plain_measure(self, text: str, size: float) -> float:
        if not text:
            return 0.0
        return self._text_measure(text, size, 1.0)

    def measure(self, text: str, size: float) -> float:
        if not text:
            return 0.0
        if not self.has_emoji_font:
            return self._text_measure(text, size, 1.0)
        return self._text_measure(text, size, EMOJI_SCALE)

    def line_metrics(self, text: str, size: float) -> tuple[float, float, float]:
        """返回 (宽度, ascent, descent)，单位是逻辑像素，descent 为正。"""
        font = self.font(size)
        try:
            ascent, descent = font.getmetrics()
        except Exception:
            ascent, descent = int(size), int(size * 0.25)
        ascent_l = ascent / OUTPUT_SCALE
        descent_l = descent / OUTPUT_SCALE
        return self.measure(text, size), ascent_l, descent_l


def _line_tokens(text: str) -> list[str]:
    """ASCII 字母数字连成一段，其余逐字，跟原版 lineTokens 一致。"""
    tokens: list[str] = []
    latin: list[str] = []
    for char in text:
        if char.isascii() and (char.isalpha() or char.isdigit()):
            latin.append(char)
            continue
        if latin:
            tokens.append("".join(latin))
            latin = []
        tokens.append(char)
    if latin:
        tokens.append("".join(latin))
    return tokens


@dataclass
class TextLine:
    text: str
    width: float
    baseline: float


@dataclass
class TextLayout:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    lines: list[TextLine] = field(default_factory=list)


@dataclass
class SegmentLayout:
    type: str = "text"
    kind: str = ""
    text: str = ""
    transcript: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    lines: list[TextLine] = field(default_factory=list)
    image: Any = None
    animation: list[tuple[Any, int]] | None = None
    radius: float = IMAGE_RADIUS


@dataclass
class ReplyLayout:
    bar_x: float = 0.0
    bar_y: float = 0.0
    bar_w: float = 0.0
    bar_h: float = 0.0
    nickname: TextLayout = field(default_factory=TextLayout)
    segments: list[SegmentLayout] = field(default_factory=list)
    reply: "ReplyLayout | None" = None


@dataclass
class BadgeLayout:
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = BADGE_HEIGHT


@dataclass
class HonorLayout:
    text: str = ""
    glyph: str = ""
    color: str = ""
    image: Any = None
    x: float = 0.0
    y: float = 0.0


@dataclass
class ReactionLayout:
    count: str = ""
    emoji: str = ""
    image: Any = None
    animation: list | None = None
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = REACTION_HEIGHT


@dataclass
class RowLayout:
    height: float = 0.0
    avatar_x: float = 0.0
    avatar_y: float = 0.0
    avatar: Any = None
    pendant: Any = None
    nickname: TextLayout = field(default_factory=TextLayout)
    badges: list[BadgeLayout] = field(default_factory=list)
    honors: list[HonorLayout] = field(default_factory=list)
    reactions: list[ReactionLayout] = field(default_factory=list)
    bubble_x: float = 0.0
    bubble_y: float = 0.0
    bubble_w: float = 0.0
    bubble_h: float = 0.0
    reply: ReplyLayout | None = None
    segments: list[SegmentLayout] = field(default_factory=list)


@dataclass
class GroupHeader:
    name: str = ""
    avatar: Any = None
    avatar_x: float = CARD_PAD_X
    avatar_y: float = CARD_PAD_Y
    name_x: float = 0.0
    name_y: float = 0.0
    lines: list[TextLine] = field(default_factory=list)


@dataclass
class TimeSeparator:
    text: str = ""
    y: float = 0.0
    line: TextLine = field(default_factory=TextLine)


@dataclass
class CardLayout:
    width: float = 0.0
    height: float = 0.0
    rows: list[RowLayout] = field(default_factory=list)
    divider_y: float = 0.0
    separators: list[TimeSeparator] = field(default_factory=list)
    header: GroupHeader | None = None
    timestamp: TextLine | None = None
    timestamp_y: float = 0.0


# ------------------------------------------------------------------ 布局


class LayoutEngine:
    def __init__(self, fonts: FontManager) -> None:
        self.fonts = fonts

    def _text_line(self, text: str, size: float, line_height: float) -> TextLine:
        width, ascent, descent = self.fonts.line_metrics(text, size)
        baseline = (line_height - (ascent + descent)) / 2.0 + ascent
        return TextLine(text=text, width=width, baseline=baseline)

    def wrap_text(
        self, text: str, max_width: float, size: float, line_height: float
    ) -> list[TextLine]:
        lines: list[TextLine] = []
        for paragraph in str(text).split("\n"):
            if paragraph == "":
                lines.append(self._text_line("", size, line_height))
                continue
            current = ""
            for token in _line_tokens(paragraph):
                if current and self.fonts.measure(current + token, size) > max_width:
                    lines.append(self._text_line(current, size, line_height))
                    current = ""
                if self.fonts.measure(token, size) <= max_width:
                    current += token
                    continue
                for char in token:
                    if current and self.fonts.measure(current + char, size) > max_width:
                        lines.append(self._text_line(current, size, line_height))
                        current = ""
                    current += char
            lines.append(self._text_line(current, size, line_height))
        return lines

    def layout(
        self,
        messages: list[dict],
        group: dict | None = None,
        timestamp: str = "",
    ) -> CardLayout:
        card = CardLayout(width=CARD_PAD_X * 2, height=CARD_PAD_Y * 2)
        available = CARD_MAX_WIDTH - CARD_PAD_X * 2 - AVATAR_SIZE - ROW_GAP
        max_row_width = 0.0
        y = CARD_PAD_Y
        header: GroupHeader | None = None
        if group and str(group.get("name") or "").strip():
            name_line = self._text_line(
                str(group.get("name") or "").strip(),
                GROUP_NAME_SIZE,
                GROUP_NAME_HEIGHT,
            )
            header = GroupHeader(
                name=name_line.text,
                avatar=group.get("avatar"),
                avatar_x=CARD_PAD_X,
                avatar_y=CARD_PAD_Y,
                name_x=CARD_PAD_X + GROUP_AVATAR_SIZE + GROUP_HEADER_GAP,
                name_y=CARD_PAD_Y,
                lines=[name_line],
            )
            header_h = max(GROUP_AVATAR_SIZE, GROUP_NAME_HEIGHT) + GROUP_HEADER_MARGIN
            y = CARD_PAD_Y + header_h
            # 分割线放在群标题下面、时间上面
            card.divider_y = y - GROUP_HEADER_MARGIN / 2.0
            max_row_width = max(
                max_row_width,
                AVATAR_SIZE + ROW_GAP + 0.0,
                GROUP_AVATAR_SIZE + GROUP_HEADER_GAP + name_line.width,
            )
        time_line: TextLine | None = None
        time_y = y
        if timestamp:
            time_line = self._text_line(timestamp, TIME_FONT_SIZE, TIME_HEIGHT)
            time_y = y
            y += TIME_HEIGHT + TIME_MARGIN
            max_row_width = max(max_row_width, time_line.width)
        rows: list[RowLayout] = []
        prev_time = 0
        for message in messages:
            msg_time = _as_int_local(message.get("time"), 0)
            if (
                rows
                and msg_time
                and prev_time
                and msg_time - prev_time > TIME_GAP_SECONDS
            ):
                label = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(msg_time))
                line = self._text_line(label, TIME_FONT_SIZE, TIME_HEIGHT)
                card.separators.append(TimeSeparator(text=label, y=y, line=line))
                y += TIME_HEIGHT + TIME_MARGIN
                max_row_width = max(max_row_width, line.width)
            if msg_time:
                prev_time = msg_time
            row = self._layout_row(message, available, y)
            if self._needs_max_width(message, available - BUBBLE_PAD_X * 2):
                row.bubble_w = available
            rows.append(row)
            row_width = AVATAR_SIZE + ROW_GAP + max(
                row.nickname.width, row.bubble_w
            )
            max_row_width = max(max_row_width, row_width)
            y += row.height + ROW_MARGIN
        if rows:
            card.height = y - ROW_MARGIN + CARD_PAD_Y
            card.width = min(CARD_MAX_WIDTH, CARD_PAD_X * 2 + max_row_width)
        card.rows = rows
        card.header = header
        card.timestamp = time_line
        card.timestamp_y = time_y
        return card

    def _needs_max_width(self, message: dict, inner_width: float) -> bool:
        if self._segments_need_max_width(message.get("segments") or [], inner_width):
            return True
        return self._reply_needs_max_width(message.get("reply"), inner_width)

    def _segments_need_max_width(
        self, segments: list[MessageSegment], max_width: float
    ) -> bool:
        for segment in segments:
            if segment.type != "text":
                continue
            for paragraph in str(segment.text).split("\n"):
                if self.fonts.measure(paragraph, TEXT_SIZE) > max_width:
                    return True
        return False

    def _reply_needs_max_width(self, reply: Any, max_width: float) -> bool:
        if reply is None:
            return False
        content_width = max_width - REPLY_BAR_WIDTH - REPLY_INDENT
        if content_width <= 0:
            return True
        if self.fonts.measure(reply.nickname, NICKNAME_SIZE) > content_width:
            return True
        if self._segments_need_max_width(reply.segments, content_width):
            return True
        return self._reply_needs_max_width(reply.reply, content_width)

    def _measure_badges(self, badges: list[str]) -> tuple[float, float]:
        """徽章组的总宽和高度，没有徽章就是 0。"""
        width = 0.0
        for text in badges:
            text = str(text or "").strip()
            if not text:
                continue
            if width > 0:
                width += BADGE_GAP
            width += self.fonts.measure(text, BADGE_FONT_SIZE) + BADGE_PAD_X * 2
        return width, BADGE_HEIGHT if width > 0 else 0.0

    def _layout_reactions(
        self, reactions: list[dict], max_width: float, x: float, y: float
    ) -> tuple[list[ReactionLayout], float, float]:
        layouts: list[ReactionLayout] = []
        cursor_x, line_y = 0.0, 0.0
        for item in reactions:
            count = str(item.get("count") or "")
            text_width = self.fonts.measure(count, REACTION_FONT_SIZE)
            width = (
                REACTION_PAD_X * 2
                + REACTION_EMOJI_SIZE
                + REACTION_EMOJI_GAP
                + text_width
            )
            if layouts and cursor_x + REACTION_GAP + width > max_width:
                cursor_x = 0.0
                line_y += REACTION_HEIGHT + REACTION_GAP
            if layouts:
                cursor_x += REACTION_GAP
            loaded = item.get("_loaded")
            layouts.append(
                ReactionLayout(
                    count=count,
                    emoji=str(item.get("emoji") or ""),
                    image=getattr(loaded, "image", None),
                    animation=getattr(loaded, "animation", None),
                    x=x + cursor_x,
                    y=y + line_y,
                    width=width,
                )
            )
            cursor_x += width
        if not layouts:
            return [], 0.0, 0.0
        used_width = max(item.x - x + item.width for item in layouts)
        used_height = line_y + REACTION_HEIGHT
        return layouts, used_width, used_height

    def _layout_badges(
        self, badges: list[str], x: float, y: float
    ) -> list[BadgeLayout]:
        layouts: list[BadgeLayout] = []
        cursor = x
        for text in badges:
            text = str(text or "").strip()
            if not text:
                continue
            width = self.fonts.measure(text, BADGE_FONT_SIZE) + BADGE_PAD_X * 2
            layouts.append(BadgeLayout(text=text, x=cursor, y=y, width=width))
            cursor += width + BADGE_GAP
        return layouts

    def _layout_row(self, message: dict, max_content_width: float, y: float) -> RowLayout:
        content_x = CARD_PAD_X + AVATAR_SIZE + ROW_GAP
        nickname_line = self._text_line(message["nickname"], NICKNAME_SIZE, NICKNAME_HEIGHT)
        max_inner = max_content_width - BUBBLE_PAD_X * 2  # 先按默认内边距估，纯图片消息下面会重算
        inner_width, inner_height = 0.0, 0.0

        # 徽章排在昵称前面（等级 → 头衔 → 昵称），一行放不下就把徽章挪到下一行
        badges: list[BadgeLayout] = []
        badge_width, badge_height = self._measure_badges(message.get("badges") or [])
        header_height = NICKNAME_HEIGHT
        nickname_x = content_x
        if badge_width > 0:
            same_line = (
                badge_width + BADGE_MARGIN + nickname_line.width <= max_content_width
            )
            if same_line:
                badge_x = content_x
                badge_y = y + (NICKNAME_HEIGHT - BADGE_HEIGHT) / 2.0
                nickname_x = content_x + badge_width + BADGE_MARGIN
            else:
                badge_x = content_x
                badge_y = y + NICKNAME_HEIGHT
                header_height = NICKNAME_HEIGHT + badge_height + BADGE_GAP
            badges = self._layout_badges(
                message.get("badges") or [], badge_x, badge_y
            )

        # 纯图片消息：气泡不留内边距，让图片自己撑满、圆角与气泡对齐
        source_segments = message.get("segments") or []
        image_only = (
            bool(source_segments)
            and message.get("reply") is None
            and all(
                seg.type == "image" and seg.kind != "emoji" for seg in source_segments
            )
        )
        pad_x = 0.0 if image_only else BUBBLE_PAD_X
        pad_y = 0.0 if image_only else BUBBLE_PAD_Y
        max_inner = max_content_width - pad_x * 2

        inner_x = content_x + pad_x
        inner_y = y + header_height + NICKNAME_MARGIN + pad_y

        reply = None
        if message.get("reply") is not None:
            reply, width, height = self._layout_reply(
                message["reply"], max_inner, inner_x, inner_y
            )
            inner_width = max(inner_width, width)
            inner_height += height
        if message.get("reply") is not None and message.get("segments"):
            inner_height += REPLY_BODY_MARGIN

        segments, width, height = self._layout_segments(
            source_segments, max_inner, inner_x, inner_y + inner_height
        )
        inner_width = max(inner_width, width)
        inner_height += height
        if image_only:
            for seg_layout in segments:
                if seg_layout.type == "image" and seg_layout.kind != "emoji":
                    seg_layout.radius = BUBBLE_RADIUS

        bubble_width = inner_width + pad_x * 2
        bubble_height = inner_height + pad_y * 2
        bubble_y = y + header_height + NICKNAME_MARGIN

        # 表情回应挂在气泡下方，不占气泡内部空间
        reactions_layout, reaction_width, reaction_height = self._layout_reactions(
            message.get("reactions") or [],
            max_content_width,
            content_x,
            bubble_y + bubble_height + REACTION_OUTER_GAP,
        )
        extra_height = (
            REACTION_OUTER_GAP + reaction_height if reactions_layout else 0.0
        )
        row_height = max(
            AVATAR_SIZE,
            header_height + NICKNAME_MARGIN + bubble_height + extra_height,
        )
        honors: list[HonorLayout] = []
        honor_text = str(message.get("honor") or "").strip()
        if honor_text:
            glyph, color = HONOR_STYLES.get(honor_text, (honor_text[:1], "#8a94a6"))
            honors.append(
                HonorLayout(
                    text=honor_text,
                    glyph=glyph,
                    color=color,
                    image=honor_icon(honor_text),
                    x=nickname_x + nickname_line.width + HONOR_MARGIN,
                    y=y + (NICKNAME_HEIGHT - HONOR_SIZE) / 2.0,
                )
            )

        header_width = nickname_x - content_x + nickname_line.width
        if honors:
            header_width += HONOR_MARGIN + HONOR_SIZE * len(honors)

        return RowLayout(
            height=row_height,
            avatar_x=CARD_PAD_X,
            avatar_y=y,
            avatar=message.get("avatar"),
            pendant=message.get("pendant"),
            badges=badges,
            honors=honors,
            reactions=reactions_layout,
            nickname=TextLayout(
                x=nickname_x,
                y=y,
                width=header_width,
                height=NICKNAME_HEIGHT,
                lines=[nickname_line],
            ),
            bubble_x=content_x,
            bubble_y=bubble_y,
            bubble_w=bubble_width,
            bubble_h=bubble_height,
            reply=reply,
            segments=segments,
        )

    def _layout_reply(
        self, reply: ReplyMessage, max_width: float, x: float, y: float
    ) -> tuple[ReplyLayout, float, float]:
        layout = ReplyLayout()
        content_x = x + REPLY_BAR_WIDTH + REPLY_INDENT
        content_width = max_width - REPLY_BAR_WIDTH - REPLY_INDENT
        cursor_y = y
        used_width = 0.0

        if reply.reply is not None:
            layout.reply, width, height = self._layout_reply(
                reply.reply, content_width, content_x, cursor_y
            )
            used_width = max(used_width, width)
            cursor_y += height + SEGMENT_MARGIN

        if reply.nickname:
            lines = self.wrap_text(
                reply.nickname, content_width, NICKNAME_SIZE, NICKNAME_HEIGHT
            )
            nickname_layout = TextLayout(
                x=content_x,
                y=cursor_y,
                height=len(lines) * NICKNAME_HEIGHT,
                lines=lines,
            )
            for line in lines:
                nickname_layout.width = max(nickname_layout.width, line.width)
            layout.nickname = nickname_layout
            used_width = max(used_width, nickname_layout.width)
            cursor_y += nickname_layout.height + NICKNAME_MARGIN

        segments, width, height = self._layout_segments(
            reply.segments, content_width, content_x, cursor_y
        )
        layout.segments = segments
        used_width = max(used_width, width)
        cursor_y += height
        total_height = cursor_y - y
        layout.bar_x = x
        layout.bar_y = y
        layout.bar_w = REPLY_BAR_WIDTH
        layout.bar_h = total_height
        return layout, REPLY_BAR_WIDTH + REPLY_INDENT + used_width, total_height

    def _layout_segments(
        self, segments: list[MessageSegment], max_width: float, x: float, y: float
    ) -> tuple[list[SegmentLayout], float, float]:
        layouts: list[SegmentLayout] = []
        used_width, used_height = 0.0, 0.0
        index = 0
        while index < len(segments):
            end = _inline_group_end(segments, index)
            if end > index:
                if layouts:
                    used_height += SEGMENT_MARGIN
                group, width, height = self._layout_inline_group(
                    segments[index:end], max_width, x, y + used_height
                )
                layouts.extend(group)
                used_width = max(used_width, width)
                used_height += height
                index = end
                continue

            segment = segments[index]
            if layouts:
                used_height += SEGMENT_MARGIN
            layout = SegmentLayout(type=segment.type, kind=segment.kind)
            if segment.type == "text":
                layout.lines = self.wrap_text(
                    segment.text, max_width, TEXT_SIZE, TEXT_LINE_HEIGHT
                )
                for line in layout.lines:
                    layout.width = max(layout.width, line.width)
                layout.height = max(1, len(layout.lines)) * TEXT_LINE_HEIGHT
            elif segment.type == "record":
                layout.type = "record"
                layout.kind = "voice"
                layout.text = str(segment.text or "").strip()
                try:
                    seconds = int(float(str(segment.text or "0") or 0))
                except (TypeError, ValueError):
                    seconds = 0
                layout.width = min(
                    VOICE_MAX_WIDTH,
                    VOICE_MIN_WIDTH + max(0, min(seconds, 60)) * 1.6,
                )
                layout.height = TEXT_LINE_HEIGHT
                transcript = _normalize_cn_punct(
                    str(segment.transcript or "").strip()
                )
                if transcript:
                    layout.transcript = transcript
                    layout.lines = self.wrap_text(
                        transcript, max_width, VOICE_TRANS_SIZE, VOICE_TRANS_LINE
                    )
                    layout.width = max(
                        layout.width,
                        max(line.width for line in layout.lines),
                    )
                    layout.height += 2.0 + len(layout.lines) * VOICE_TRANS_LINE
            elif segment.type in ("image", "face"):
                max_w, max_h = 200.0, 200.0
                if segment.kind == "emoji":
                    max_w = max_h = INLINE_EMOJI_SIZE
                elif segment.kind == "sticker":
                    max_w = max_h = 128.0
                image = segment_image(segment)
                if image is not None and image.width > 0 and image.height > 0:
                    scale = min(
                        1.0,
                        max_w / image.width,
                        max_h / image.height,
                    )
                    layout.width = image.width * scale
                    layout.height = image.height * scale
                    if segment.kind == "emoji":
                        layout.width = layout.height = INLINE_EMOJI_SIZE
                    layout.image = image.image
                    layout.animation = image.animation
                else:
                    if segment.kind == "emoji" and segment.text:
                        placeholder = segment.text
                    elif segment.type == "face":
                        placeholder = "[表情]"
                    else:
                        placeholder = "[图片]"
                    layout.type = "text"
                    layout.lines = [
                        self._text_line(placeholder, TEXT_SIZE, TEXT_LINE_HEIGHT)
                    ]
                    layout.width = layout.lines[0].width
                    layout.height = TEXT_LINE_HEIGHT
            layout.x = x
            layout.y = y + used_height
            used_width = max(used_width, layout.width)
            used_height += layout.height
            layouts.append(layout)
            index += 1
        return layouts, used_width, used_height

    def _layout_inline_group(
        self, group: list[MessageSegment], max_width: float, x: float, y: float
    ) -> tuple[list[SegmentLayout], float, float]:
        layouts: list[SegmentLayout] = []
        cursor_x, line_y, used_width = 0.0, 0.0, 0.0
        previous_emoji = False

        def new_line() -> None:
            nonlocal cursor_x, line_y, used_width, previous_emoji
            used_width = max(used_width, cursor_x)
            cursor_x = 0.0
            line_y += TEXT_LINE_HEIGHT
            previous_emoji = False

        for segment in group:
            if segment.type in ("image", "face"):
                gap = INLINE_EMOJI_GAP if cursor_x > 0 else 0.0
                if cursor_x + gap + INLINE_EMOJI_SIZE > max_width:
                    new_line()
                    gap = 0.0
                cursor_x += gap
                image = segment_image(segment)
                if image is None or image.image is None:
                    # 图没下到就退回文字，别在行里留一个空洞
                    placeholder = segment.text or "?"
                    line = self._text_line(placeholder, TEXT_SIZE, TEXT_LINE_HEIGHT)
                    layouts.append(
                        SegmentLayout(
                            type="text",
                            x=x + cursor_x,
                            y=y + line_y,
                            width=line.width,
                            height=TEXT_LINE_HEIGHT,
                            lines=[line],
                        )
                    )
                    cursor_x += line.width
                    previous_emoji = False
                    continue
                layouts.append(
                    SegmentLayout(
                        type="image",
                        kind=segment.kind,
                        x=x + cursor_x,
                        y=y + line_y,
                        width=INLINE_EMOJI_SIZE,
                        height=INLINE_EMOJI_SIZE,
                        image=image.image,
                        animation=image.animation,
                    )
                )
                cursor_x += INLINE_EMOJI_SIZE
                previous_emoji = True
                continue

            paragraphs = str(segment.text).split("\n")
            for paragraph_index, paragraph in enumerate(paragraphs):
                if paragraph_index > 0:
                    new_line()
                fragment = ""

                def flush() -> None:
                    nonlocal fragment, cursor_x, previous_emoji
                    if not fragment:
                        return
                    if previous_emoji and cursor_x > 0:
                        cursor_x += INLINE_EMOJI_GAP
                    line = self._text_line(fragment, TEXT_SIZE, TEXT_LINE_HEIGHT)
                    layouts.append(
                        SegmentLayout(
                            type="text",
                            x=x + cursor_x,
                            y=y + line_y,
                            width=line.width,
                            height=TEXT_LINE_HEIGHT,
                            lines=[line],
                        )
                    )
                    cursor_x += line.width
                    fragment = ""
                    previous_emoji = False

                for token in _line_tokens(paragraph):
                    gap = INLINE_EMOJI_GAP if (previous_emoji and cursor_x > 0) else 0.0
                    if (
                        self.fonts.measure(fragment + token, TEXT_SIZE)
                        <= max_width - cursor_x - gap
                    ):
                        fragment += token
                        continue
                    flush()
                    if cursor_x > 0:
                        new_line()
                    if self.fonts.measure(token, TEXT_SIZE) <= max_width:
                        fragment = token
                        continue
                    for char in token:
                        if (
                            fragment
                            and self.fonts.measure(fragment + char, TEXT_SIZE) > max_width
                        ):
                            flush()
                            new_line()
                        fragment += char
                flush()

        used_width = max(used_width, cursor_x)
        return layouts, used_width, line_y + TEXT_LINE_HEIGHT


def _inline_group_end(segments: list[MessageSegment], start: int) -> int:
    end = start
    has_text, has_emoji = False, False
    while end < len(segments):
        segment = segments[end]
        if segment.type == "text":
            has_text = True
        elif (segment.type == "image" and segment.kind == "emoji") or segment.type == "face":
            has_emoji = True
        else:
            break
        end += 1
    return end if (has_text and has_emoji) else start


# ------------------------------------------------------------------ 资源


def _decode_animation(img: Any) -> list[tuple[Any, int]] | None:
    """多帧 GIF / APNG 解成 [(帧, 时长厘秒)]，非动图返回 None。"""
    try:
        if not getattr(img, "is_animated", False):
            return None
        frames: list[tuple[Any, int]] = []
        for index in range(int(getattr(img, "n_frames", 1))):
            img.seek(index)
            frame = img.convert("RGBA").copy()
            delay = int(img.info.get("duration") or 0) or 100
            frames.append((frame, max(2, int(round(delay / 10)))))
        if len(frames) < 2:
            return None
        return frames
    except Exception as e:
        logger.debug(f"[reply_guard] 解码动图失败: {e}")
        return None


def _decode_base64(payload: str) -> bytes | None:
    try:
        import base64

        return base64.b64decode(payload, validate=False)
    except Exception:
        return None


def _decode_data_url(source: str) -> bytes | None:
    try:
        header, payload = source.split(",", 1)
    except ValueError:
        return None
    if ";base64" not in header.lower():
        return None
    return _decode_base64(payload)


def _local_path(source: str) -> str | None:
    """把 file:// 或本地绝对路径还原成路径；不是本地形式就返回 None。"""
    text = (source or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(text)
        path = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path = f"//{parsed.netloc}{path}"
        if re.match(r"^/[A-Za-z]:", path):  # /C:/Users/... -> C:/Users/...
            path = path[1:]
        return path or None
    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\") or text.startswith("/"):
        return text
    return None


def _read_local_file(path: str) -> bytes | None:
    try:
        if not os.path.isfile(path):
            return None
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            return None
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as e:
        logger.debug(f"[reply_guard] 读本地图片失败 {path}: {e}")
        return None


class ResourceLoader:
    def __init__(self, proxy: str = "", cache_dir: str = "") -> None:
        self.proxy = proxy
        self.cache_dir = cache_dir
        self._cache: dict[str, LoadedImage] = {}

    async def _fetch(self, source: str) -> bytes | None:
        source = (source or "").strip()
        if not source:
            return None
        lowered = source.lower()

        # data:image/png;base64,xxxx
        if lowered.startswith("data:"):
            return _decode_data_url(source)

        # OneBot 常见的 base64://xxxx 写法
        if lowered.startswith("base64://"):
            return _decode_base64(source[len("base64://") :])

        # 本地路径 / file://（NapCat 的缓存图常常只给这个）
        local = _local_path(source)
        if local is not None:
            data = _read_local_file(local)
            if data:
                return data
            logger.debug(f"[reply_guard] 本地图片读不到: {local}")
            return None

        if not lowered.startswith(_REMOTE_PREFIXES):
            logger.debug(f"[reply_guard] 认不出的图片地址，跳过: {source[:120]}")
            return None

        try:
            import aiohttp
        except ImportError:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(
                timeout=timeout, headers=IMAGE_HEADERS
            ) as session:
                async with session.get(
                    source, proxy=self.proxy or None, allow_redirects=True
                ) as resp:
                    if resp.status // 100 != 2:
                        logger.debug(
                            f"[reply_guard] 图片 HTTP {resp.status}: {source[:120]}"
                        )
                        return None
                    data = await resp.read()
                    if len(data) > MAX_IMAGE_BYTES:
                        logger.debug(f"[reply_guard] 图片超过 16MB，跳过: {source[:120]}")
                        return None
                    return data
        except Exception as e:
            logger.debug(f"[reply_guard] 下载图片失败 {source[:120]}: {e}")
            return None

    async def load(self, source: str) -> LoadedImage:
        source = (source or "").strip()
        if not source:
            return LoadedImage()
        cached = self._cache.get(source)
        if cached is not None:
            return cached
        data = await self._fetch(source)
        if not data:
            result = LoadedImage()
        else:
            try:
                from io import BytesIO

                img = Image.open(BytesIO(data))
                img.load()
                animation = _decode_animation(img)
                result = LoadedImage(
                    image=img.convert("RGBA"),
                    width=img.width,
                    height=img.height,
                    animation=animation,
                )
            except Exception as e:
                logger.debug(f"[reply_guard] 解码图片失败: {e}")
                result = LoadedImage()
        self._cache[source] = result
        return result


def segment_image(segment: MessageSegment) -> LoadedImage | None:
    image = getattr(segment, "_image", None)
    return image if isinstance(image, LoadedImage) else None


_honor_icon_cache: dict[str, Any] = {}


def honor_icon_dir() -> str:
    """互动标识图标放这里：data/plugin_data/<插件>/icons/honor_龙王.png"""
    here = os.path.dirname(os.path.abspath(__file__))
    plugin_root = os.path.dirname(here)
    data_root = os.path.dirname(os.path.dirname(plugin_root))
    candidates = [
        os.path.join(data_root, "plugin_data", os.path.basename(plugin_root), "icons"),
        os.path.join(plugin_root, "icons"),
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    return ""


def honor_icon(text: str) -> Any:
    """按名字找一张标识图；找不到返回 None，外面会退回画圆标。"""
    key = str(text or "").strip()
    if not key:
        return None
    if key in _honor_icon_cache:
        return _honor_icon_cache[key]

    path_dir = honor_icon_dir()
    image = None
    if path_dir and Image is not None:
        try:
            names = [
                name
                for name in os.listdir(path_dir)
                if name.lower().endswith((".png", ".webp", ".jpg", ".gif"))
                and key in name
            ]
            if names:
                names.sort(key=len)
                with Image.open(os.path.join(path_dir, names[0])) as handle:
                    image = handle.convert("RGBA").copy()
                logger.info(f"[reply_guard] 互动标识图标：{names[0]}")
        except Exception as e:
            logger.debug(f"[reply_guard] 读互动标识图标失败 {key}: {e}")
    _honor_icon_cache[key] = image
    return image


def _level_label(level: str) -> str:
    """群等级：纯数字就写成 Lv.9，别的原样显示。"""
    text = str(level or "").strip()
    if not text:
        return ""
    return f"LV{text}" if text.isdigit() else text


# 常用 Unicode emoji → QQ 表情 id。QQ 里这些是自家那套亮晶晶的图，
# 用系统彩色字体或 Twemoji 画出来都不像，所以认得出来就用 QQ 的表情图。
_QQ_FACE_BY_CHAR = {
    "😂": "182",
    "🤣": "193",
    "😁": "13",
    "😄": "13",
    "😊": "21",
    "🙂": "14",
    "😭": "9",
    "😢": "107",
    "😓": "96",
    "😅": "27",
    "😡": "11",
    "😠": "326",
    "😏": "101",
    "🙄": "22",
    "😴": "8",
    "😪": "25",
    "😱": "26",
    "😨": "110",
    "😰": "96",
    "👍": "76",
    "👎": "77",
    "❤": "66",
    "💔": "67",
    "🎉": "320",
    "🌹": "63",
    "🍉": "89",
    "☀": "74",
    "🌙": "75",
    "🎁": "69",
    "🍰": "53",
    "☕": "60",
    "🍺": "113",
    "💩": "59",
    "⚡": "54",
    "💣": "55",
    "⚽": "57",
    "👏": "99",
    "🤝": "78",
    "✌": "79",
    "🙏": "145",
    "🐷": "46",
    "👻": "187",
    "💰": "158",
    "💊": "168",
    "🔫": "169",
    "🍵": "171",
    "🐶": "277",
    "😷": "247",
    "🎆": "333",
    "🤦": "264",
    "🥰": "21",
    "🥲": "107",
}

_TWEMOJI_URL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{name}.png"
_EMOJI_PACK_URL = (
    "https://cdn.jsdelivr.net/npm/emoji-datasource-{pack}@15.1.2/img/{pack}/64/{cp}.png"
)

# emoji 画风：qq=QQ 自己的表情图，google=Noto，apple=苹果，twemoji=扁平，
# font=交给系统字体。默认不跟 QQ 撞脸。
EMOJI_STYLES = ("google", "apple", "qq", "twemoji", "font")
_emoji_style = "apple"


def set_emoji_style(style: str) -> str:
    """设置这一次渲染用哪套 emoji 图，返回实际生效的值。"""
    global _emoji_style
    value = str(style or "").strip().lower()
    if value not in EMOJI_STYLES:
        value = "apple"
    _emoji_style = value
    return value


def _emoji_codepoints(chars: str, keep_vs: bool = False) -> str:
    """😂 -> 1f602；组合表情用下划线连。

    emoji-datasource 那套图用的是「完整序列」，❤️ 要写成 2764_fe0f；
    Twemoji 反过来只认 2764。所以这个开关交给调用方决定。
    """
    points = [
        f"{ord(ch):x}"
        for ch in (chars or "")
        if keep_vs or ord(ch) != 0xFE0F
    ]
    return "-".join(points)


def _emoji_asset_url(chars: str) -> str:
    """按当前画风给出这张 emoji 图的地址，返回空串表示交给字体画。"""
    style = _emoji_style
    if style == "font" or not chars:
        return ""
    if style == "qq":
        face_id = _qq_face_id(chars)
        if face_id:
            return f"{QFACE_BASE_URL}/{face_id}/apng/{face_id}.png"
        style = "google"  # QQ 没有的，用 Noto 顶上
    if style == "twemoji":
        return _TWEMOJI_URL.format(name=_emoji_name(chars))
    return _EMOJI_PACK_URL.format(
        pack=style, cp=_emoji_codepoints(chars, keep_vs=True)
    )

_EMOJI_SEQ_RE = re.compile(
    "(?:"
    "[\U0001F1E6-\U0001F1FF]{2}"
    "|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF]"
    "\uFE0F?"
    "[\U0001F3FB-\U0001F3FF]?"
    "(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
    "\uFE0F?[\U0001F3FB-\U0001F3FF]?)*"
    ")"
)


def _emoji_name(chars: str) -> str:
    codepoints = [ord(char) for char in chars if ord(char) != 0xFE0F]
    return "-".join(f"{cp:x}" for cp in codepoints)


# 箭头、几何图形这些只是符号，不是表情，别拿去当 emoji 图（多半 404，画出来是空白）
_TEXT_SYMBOL_RE = re.compile("[\u2190-\u21FF\u2300-\u23FF\u25A0-\u25FF\u2B00-\u2BFF]")


def _qq_face_id(chars: str) -> str:
    """认得出来的 emoji 换成 QQ 表情 id，认不出返回空串。"""
    key = (chars or "").replace("\ufe0f", "").replace("\u200d", "")
    if key in _QQ_FACE_BY_CHAR:
        return _QQ_FACE_BY_CHAR[key]
    return _QQ_FACE_BY_CHAR.get(key[:1], "")


def _split_text_emoji(text: str) -> list[MessageSegment]:
    """把文本里的 emoji 拆成图片段，交给内联表情排版。"""
    segments: list[MessageSegment] = []
    cursor = 0
    for match in _EMOJI_SEQ_RE.finditer(text):
        start, end = match.span()
        if start > cursor:
            segments.append(MessageSegment(type="text", text=text[cursor:start]))
        if all(_TEXT_SYMBOL_RE.match(ch) for ch in match.group()):
            segments.append(MessageSegment(type="text", text=match.group()))
            cursor = end
            continue
        url = _emoji_asset_url(match.group())
        if not url:
            # font 模式：原样留着，让字体去画
            segments.append(MessageSegment(type="text", text=match.group()))
            cursor = end
            continue
        segments.append(
            MessageSegment(type="image", kind="emoji", url=url, text=match.group())
        )
        cursor = end
    if cursor < len(text):
        segments.append(MessageSegment(type="text", text=text[cursor:]))
    return segments or [MessageSegment(type="text", text=text)]


def _expand_segments(segments: list[MessageSegment]) -> list[MessageSegment]:
    """把文本里的 emoji 拆成图片段；顺便丢掉会画成方块的变体选择符。"""
    expanded: list[MessageSegment] = []
    for segment in segments:
        if segment.type == "text" and segment.text:
            for piece in _split_text_emoji(segment.text):
                if piece.type == "text" and piece.text:
                    piece.text = piece.text.replace("\ufe0f", "")
                    if not piece.text:
                        continue
                    if expanded and expanded[-1].type == "text":
                        # 拆出来的文字要接回去，不然每段都会占一行
                        expanded[-1].text = (expanded[-1].text or "") + piece.text
                        continue
                expanded.append(piece)
        else:
            expanded.append(segment)
    return expanded


_CQ_CODE_RE = re.compile(r"\[CQ:([a-zA-Z_]+),?([^\]]*)\]")

# CQ 串里这几个字符是转义过的，不还原的话 & [ ] , 会原样显示出来
_CQ_ESCAPES = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


def _unescape_cq(text: str) -> str:
    for src, dst in _CQ_ESCAPES:
        text = text.replace(src, dst)
    return text


def _merge_text_segments(segments: list[MessageSegment]) -> list[MessageSegment]:
    """相邻的文本段合并成一段，免得被排版拆成多行。"""
    merged: list[MessageSegment] = []
    for segment in segments:
        if segment.type == "text":
            if not (segment.text or ""):
                continue
            if merged and merged[-1].type == "text":
                merged[-1].text = (merged[-1].text or "") + (segment.text or "")
                continue
        merged.append(segment)
    return merged


def _parse_cq_string(text: str) -> tuple[list[MessageSegment], str | None]:
    """解析 [CQ:xxx,key=value] 形式的消息串（get_msg 返回的就是这种）。"""
    segments: list[MessageSegment] = []
    reply_id: str | None = None
    cursor = 0
    for match in _CQ_CODE_RE.finditer(text or ""):
        if match.start() > cursor:
            plain = text[cursor : match.start()]
            if plain:
                segments.append(MessageSegment(type="text", text=_unescape_cq(plain)))
        code = match.group(1)
        params: dict[str, str] = {}
        for pair in match.group(2).split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                params[key.strip()] = value.strip()

        if code == "at":
            qq = str(params.get("qq") or "")
            if qq == "all":
                segments.append(MessageSegment(type="text", text="@全体成员"))
            elif qq:
                segments.append(MessageSegment(type="at", id=qq))
        elif code == "image":
            url = str(params.get("url") or "")
            file = str(params.get("file") or "")
            source = url or file
            if source:
                segments.append(
                    MessageSegment(type="image", kind="image", url=source, id=file)
                )
            else:
                logger.debug("[reply_guard] CQ 图片段既没有 url 也没有 file")
                segments.append(MessageSegment(type="text", text="[图片]"))
        elif code == "face":
            face_id = str(params.get("id") or "")
            if face_id.isdigit():
                segments.append(MessageSegment(type="face", kind="emoji", id=face_id))
            else:
                segments.append(MessageSegment(type="text", text="[表情]"))
        elif code == "reply":
            reply_id = str(params.get("id") or "") or None
        elif code == "json" or code == "xml":
            segments.append(MessageSegment(type="text", text="[卡片消息]"))
        cursor = match.end()

    if cursor < len(text or ""):
        tail = text[cursor:]
        if tail:
            segments.append(MessageSegment(type="text", text=_unescape_cq(tail)))
    return _merge_text_segments(segments), reply_id


def _segment_urls(segment: MessageSegment) -> list[str]:
    """图片段和 QQ 表情要取的地址：图片先试 url 再试 file，表情优先 apng。"""
    if segment.type == "image":
        candidates: list[str] = []
        for item in (segment.url, segment.id):
            item = str(item or "").strip()
            if item and item not in candidates:
                candidates.append(item)
        return candidates
    face_id = str(segment.id or "")
    if segment.type == "face" and face_id.isdigit():
        return [
            f"{QFACE_BASE_URL}/{face_id}/apng/{face_id}.png",
            f"{QFACE_BASE_URL}/{face_id}/png/{face_id}.png",
        ]
    return []


async def _load_first(
    loader: ResourceLoader,
    urls: list[str],
    segment: Any = None,
    resolver: Any = None,
) -> LoadedImage:
    for url in urls:
        image = await loader.load(url)
        if image.image is not None:
            return image
    # 全都没拿到，最后交给插件层的兜底（比如 OneBot 的 get_image）
    if resolver is not None and isinstance(segment, MessageSegment):
        fallback = ""
        try:
            fallback = await resolver(segment)
        except Exception as e:
            logger.debug(f"[reply_guard] 图片兜底解析失败: {e}")
        if fallback:
            image = await loader.load(fallback)
            if image.image is not None:
                logger.debug(f"[reply_guard] 图片兜底成功: {str(fallback)[:120]}")
                return image
    return LoadedImage()


async def prepare_messages(
    loader: ResourceLoader,
    messages: Sequence[QuoteMessage],
    resolver: Any = None,
) -> list[dict]:
    """把消息转成布局用的结构，顺便并发下好头像和图片。"""
    prepared: list[dict] = []
    tasks: list[tuple[Any, str, list[str]]] = []

    def register(owner: Any, attr: str, urls: list[str]) -> None:
        urls = [u for u in urls if u]
        if urls:
            tasks.append((owner, attr, urls))

    def walk_reply(reply: ReplyMessage | None, depth: int = 0) -> ReplyMessage | None:
        if reply is None or depth > MAX_REPLY_DEPTH:
            return None
        reply.nickname = reply.nickname or "匿名"
        if not reply.segments:
            reply.segments = [
                MessageSegment(type="text", text=REPLY_UNAVAILABLE_TEXT)
            ]
        reply.segments = _expand_segments(_merge_text_segments(list(reply.segments)))
        for segment in reply.segments:
            register(segment, "_image", _segment_urls(segment))
        reply.reply = walk_reply(reply.reply, depth + 1)
        return reply

    for message in messages:
        avatar_url = message.avatar or (
            AVATAR_URL.format(qq=message.user_id)
            if str(message.user_id).isdigit()
            else ""
        )
        # 等级和群头衔合成同一个色块：LV33 小区区
        badge_text = " ".join(
            part
            for part in (
                _level_label(message.level),
                str(message.title or "").strip(),
            )
            if part
        )
        badges = [badge_text] if badge_text else []
        reactions: list[dict] = []
        for raw in message.reactions or []:
            if not isinstance(raw, dict):
                continue
            eid = str(raw.get("emoji_id") or "").strip()
            try:
                count = int(raw.get("likes_cnt") or 0)
            except (TypeError, ValueError):
                count = 0
            if not eid or count <= 0:
                continue
            entry: dict[str, Any] = {"count": count, "emoji": "", "_image": None}
            if eid.isdigit():
                # 一律先按 QQ 表情 id 取图，这样一排小黄脸才是同一种画风；
                # 取不到才退化成 Unicode emoji。
                entry["_urls"] = [
                    f"{QFACE_BASE_URL}/{eid}/apng/{eid}.png",
                    f"{QFACE_BASE_URL}/{eid}/png/{eid}.png",
                ]
                if int(eid) > 1000:
                    try:
                        char = chr(int(eid))
                    except (ValueError, OverflowError):
                        char = ""
                    url = _emoji_asset_url(char) if char else ""
                    if url:
                        entry["_urls"] = [url]
                    entry["emoji"] = char
            reactions.append(entry)
        item: dict[str, Any] = {
            "nickname": message.nickname or "匿名",
            "avatar": None,
            "pendant": None,
            "badges": badges,
            "honor": str(message.honor or "").strip(),
            "time": int(message.time or 0),
            "reactions": reactions,
            "segments": _expand_segments(
                _merge_text_segments(list(message.segments))
            ),
            "reply": None,
        }
        if avatar_url:
            register(item, "avatar", [avatar_url])
        if str(message.pendant or "").strip():
            register(item, "pendant", [str(message.pendant).strip()])
        for entry in reactions:
            register(entry, "_loaded", entry.get("_urls") or [])
        for segment in item["segments"]:
            register(segment, "_image", _segment_urls(segment))
        item["reply"] = walk_reply(message.reply) if message.reply else None
        prepared.append(item)

    if tasks:
        results = await asyncio.gather(
            *[
                _load_first(loader, urls, owner, resolver)
                for owner, attr, urls in tasks
            ],
            return_exceptions=True,
        )
        for (owner, attr, _urls), result in zip(tasks, results):
            if not isinstance(result, LoadedImage):
                continue
            if isinstance(owner, dict):
                # _loaded 要整个 LoadedImage，动图信息在里面
                owner[attr] = result if attr == "_loaded" else result.image
            else:
                setattr(owner, attr, result)

    return prepared


# ------------------------------------------------------------------ 绘制


def _luma(color: str) -> float:
    """粗略亮度，用来判断这套配色是深色还是浅色。"""
    try:
        red, green, blue = _hex(color)
    except Exception:
        return 1.0
    return (0.299 * red + 0.587 * green + 0.114 * blue) / 255.0


def _contrast_theme(theme: "CardTheme") -> "CardTheme":
    """深色配色下把没指定过的字色自动提亮，免得黑字糊在深色气泡上。"""
    if _luma(theme.card_bg) >= 0.5 and _luma(theme.bubble_bg) >= 0.5:
        return theme
    data = {name: getattr(theme, name) for name in theme.__dataclass_fields__}
    swaps = (
        ("message_color", MESSAGE_COLOR, "#e6e8ec"),
        ("reply_text_color", REPLY_TEXT_COLOR, "#b6bcc8"),
        ("nickname_color", NICKNAME_COLOR, "#9aa3b2"),
        ("badge_bg", BADGE_BG, "#333a44"),
        ("badge_color", BADGE_COLOR, "#c3cad6"),
        ("reply_bar_color", REPLY_BAR_COLOR, "#4a515c"),
        ("reaction_bg", REACTION_BG, "#333a44"),
        ("reaction_color", REACTION_COLOR, "#c3cad6"),
    )
    for name, default, dark in swaps:
        if data.get(name) == default:
            data[name] = dark
    return CardTheme(**data)


def _as_int_local(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _s(value: float) -> float:
    return value * OUTPUT_SCALE


def _flatten(img: Any, bg: str = CARD_BG) -> Any:
    """RGBA 铺到底色上，避免导出成 RGB / GIF 时透明边角变黑。"""
    if img.mode != "RGBA":
        return img.convert("RGB")
    base = Image.new("RGB", img.size, _hex(bg))
    base.paste(img, (0, 0), img)
    return base


def _circle_image(img: Any, size: int) -> Any:
    """头像按中心裁成正方形再画圆（原版是 slice，不拉伸）。"""
    img = img.convert("RGBA")
    width, height = img.size
    side = min(width, height)
    if side > 0:
        left = (width - side) // 2
        top = (height - side) // 2
        img = img.crop((left, top, left + side, top + side))
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _rounded_image(img: Any, size: tuple[int, int], radius: int) -> Any:
    img = img.convert("RGBA").resize(size, Image.LANCZOS)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255
    )
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _trim_alpha(img: Any) -> Any:
    """去掉四周透明留白，让不同来源的表情看起来一样大。"""
    try:
        img = img.convert("RGBA")
        box = img.split()[-1].getbbox()
        return img.crop(box) if box else img
    except Exception:
        return img


def _fit_image(img: Any, side: int) -> Any | None:
    """把挂件之类的小图按长边缩进 side×side，保持比例不变。"""
    if img is None or side <= 0:
        return None
    try:
        img = img.convert("RGBA")
        width, height = img.size
        if width <= 0 or height <= 0:
            return None
        scale = side / max(width, height)
        return img.resize(
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            Image.LANCZOS,
        )
    except Exception as e:
        logger.debug(f"[reply_guard] 挂件缩放失败: {e}")
        return None


def _draw_text(
    canvas: Any,
    draw: Any,
    x: float,
    baseline: float,
    text: str,
    fonts: FontManager,
    size: float,
    color: str,
) -> None:
    if not text:
        return
    main_font = fonts.font(size)
    try:
        ascent, descent = main_font.getmetrics()
    except Exception:
        ascent, descent = 0, 0

    cursor = x
    base_y = _s(baseline)
    for is_emoji, path, chunk in fonts.styled_runs(text):
        if is_emoji and fonts.has_emoji_font:
            target = max(1, int(round(_s(size) * EMOJI_SCALE)))
            for char in chunk:
                img = _render_emoji(char, target)
                if img is None:
                    cursor += size * EMOJI_SCALE
                    continue
                top = int(round(base_y + descent - target))
                canvas.paste(img, (int(round(_s(cursor))), top), img)
                cursor += size * EMOJI_SCALE
            continue
        # 主字体没有这个字的字形时，styled_runs 已经换成了回退字体
        font = _load_font(path or fonts.font_path, size)
        draw.text((_s(cursor), base_y - ascent), chunk, font=font, fill=_hex(color))
        try:
            cursor += float(font.getlength(chunk)) / OUTPUT_SCALE
        except Exception:
            cursor += len(chunk) * size * 0.6


def _draw_segments(
    canvas: Any,
    draw: Any,
    layouts: list[SegmentLayout],
    text_color: str,
    fonts: FontManager,
    skip_animation: bool = False,
) -> None:
    for segment in layouts:
        if segment.type == "text":
            for index, line in enumerate(segment.lines):
                baseline = segment.y + line.baseline + index * TEXT_LINE_HEIGHT
                _draw_text(
                    canvas, draw, segment.x, baseline, line.text, fonts, TEXT_SIZE, text_color
                )
        elif segment.type == "record":
            _draw_voice(canvas, draw, segment, text_color, fonts)
        elif segment.image is not None:
            if skip_animation and segment.animation:
                continue
            size = (max(1, int(_s(segment.width))), max(1, int(_s(segment.height))))
            if segment.kind == "emoji":
                img = _circle_image(segment.image, size[0]) if False else segment.image.convert(
                    "RGBA"
                ).resize(size, Image.LANCZOS)
            else:
                img = _rounded_image(
                    segment.image,
                    size,
                    max(1, int(_s(getattr(segment, "radius", IMAGE_RADIUS)))),
                )
            canvas.paste(img, (int(_s(segment.x)), int(_s(segment.y))), img)


VOICE_BAR_HEIGHTS = (0.35, 0.7, 0.5, 0.9, 0.45, 0.75, 0.6, 1.0, 0.4, 0.65)


def _draw_voice(canvas: Any, draw: Any, segment: Any, color: str, fonts: FontManager) -> None:
    """语音条：两根电波柱子 + 时长，够像就行。"""
    label = str(segment.text or "").strip()
    label_text = f"{label}''" if label else ""
    label_width = fonts.measure(label_text, TEXT_SIZE - 3) if label_text else 0.0
    bars_width = max(24.0, min(segment.width - label_width - 14.0, 112.0))
    count = max(3, int(bars_width // (VOICE_BAR_WIDTH + VOICE_BAR_GAP)))
    top = segment.y + (TEXT_LINE_HEIGHT - 14.0) / 2.0
    for index in range(count):
        ratio = VOICE_BAR_HEIGHTS[index % len(VOICE_BAR_HEIGHTS)]
        bar_h = 5.0 + ratio * 9.0
        x = segment.x + index * (VOICE_BAR_WIDTH + VOICE_BAR_GAP)
        y0 = top + (14.0 - bar_h) / 2.0
        draw.rounded_rectangle(
            (_s(x), _s(y0), _s(x + VOICE_BAR_WIDTH), _s(y0 + bar_h)),
            radius=int(_s(VOICE_BAR_WIDTH / 2.0)),
            fill=_hex(color),
        )
    if label_text:
        _draw_text(
            canvas,
            draw,
            segment.x + bars_width + 8.0,
            segment.y + TEXT_LINE_HEIGHT / 2.0 + (TEXT_SIZE - 3) / 2.0 - 1.0,
            label_text,
            fonts,
            TEXT_SIZE - 3,
            color,
        )
    # 语音转文字：同一个气泡里跟在下面
    for index, line in enumerate(segment.lines):
        _draw_text(
            canvas,
            draw,
            segment.x,
            segment.y + TEXT_LINE_HEIGHT + 2.0 + line.baseline
            + index * VOICE_TRANS_LINE,
            line.text,
            fonts,
            VOICE_TRANS_SIZE,
            color,
        )


def _draw_reactions(
    canvas: Any,
    draw: Any,
    reactions: list[ReactionLayout],
    fonts: FontManager,
    theme: "CardTheme",
    skip_animation: bool = False,
) -> None:
    if not reactions:
        return
    font = fonts.font(REACTION_FONT_SIZE)
    try:
        ascent, descent = font.getmetrics()
    except Exception:
        ascent, descent = 0, 0
    emoji_side = int(_s(REACTION_EMOJI_SIZE))

    chip = (theme.reaction_style or "chip").lower() != "plain"
    for item in reactions:
        if chip:
            draw.rounded_rectangle(
                (
                    _s(item.x),
                    _s(item.y),
                    _s(item.x + item.width),
                    _s(item.y + item.height),
                ),
                radius=int(_s(item.height / 2.0)),
                fill=_hex(theme.reaction_bg),
            )
        emoji_x = item.x + REACTION_PAD_X
        emoji_y = item.y + (item.height - REACTION_EMOJI_SIZE) / 2.0
        # 动图那段留给 GIF 合成，但外壳和数字现在就得画
        drawn = None
        if skip_animation and item.animation:
            item.image = None
        if item.image is not None:
            drawn = _fit_image(_trim_alpha(item.image), emoji_side)
        elif item.emoji and fonts.has_emoji_font:
            drawn = _render_emoji(item.emoji, emoji_side)
        if drawn is not None:
            center_y = _s(emoji_y + REACTION_EMOJI_SIZE / 2.0)
            canvas.paste(
                drawn,
                (
                    int(round(_s(emoji_x))),
                    int(round(center_y - drawn.height / 2.0)),
                ),
                drawn,
            )
        baseline = (
            item.y
            + (item.height - (ascent + descent) / OUTPUT_SCALE) / 2.0
            + ascent / OUTPUT_SCALE
        )
        _draw_text(
            canvas,
            draw,
            emoji_x + REACTION_EMOJI_SIZE + REACTION_EMOJI_GAP,
            baseline,
            item.count,
            fonts,
            REACTION_FONT_SIZE,
            theme.reaction_color,
        )


def _draw_reply(
    canvas: Any,
    draw: Any,
    reply: ReplyLayout | None,
    fonts: FontManager,
    skip_animation: bool = False,
    theme: "CardTheme | None" = None,
) -> None:
    if reply is None:
        return
    theme = theme or DEFAULT_THEME
    draw.rectangle(
        (
            _s(reply.bar_x),
            _s(reply.bar_y),
            _s(reply.bar_x + reply.bar_w),
            _s(reply.bar_y + reply.bar_h),
        ),
        fill=_hex(theme.reply_bar_color),
    )
    _draw_reply(canvas, draw, reply.reply, fonts, skip_animation, theme)
    for index, line in enumerate(reply.nickname.lines):
        baseline = (
            reply.nickname.y + line.baseline + index * NICKNAME_HEIGHT
        )
        _draw_text(
            canvas,
            draw,
            reply.nickname.x,
            baseline,
            line.text,
            fonts,
            NICKNAME_SIZE,
            theme.nickname_color,
        )
    _draw_segments(
        canvas, draw, reply.segments, theme.reply_text_color, fonts, skip_animation
    )


def render_card_sync(
    card: CardLayout,
    font_path: str,
    skip_animation: bool = False,
    theme: "CardTheme | None" = None,
) -> Any:
    theme = _contrast_theme(theme or DEFAULT_THEME)
    fonts = FontManager(font_path)
    width = max(1, int(math.ceil(_s(card.width))))
    height = max(1, int(math.ceil(_s(card.height))))
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=int(_s(12)),
        fill=_hex(theme.card_bg),
    )

    nickname_font = fonts.font(NICKNAME_SIZE)

    # 顶部群名 + 群头像
    if card.header is not None:
        hdr = card.header
        group_avatar = int(_s(GROUP_AVATAR_SIZE))
        draw.ellipse(
            (
                _s(hdr.avatar_x),
                _s(hdr.avatar_y),
                _s(hdr.avatar_x + GROUP_AVATAR_SIZE),
                _s(hdr.avatar_y + GROUP_AVATAR_SIZE),
            ),
            fill=_hex(AVATAR_BG),
        )
        if hdr.avatar is not None:
            img = _circle_image(hdr.avatar, group_avatar)
            canvas.paste(img, (int(_s(hdr.avatar_x)), int(_s(hdr.avatar_y))), img)
        for line in hdr.lines:
            baseline = hdr.name_y + line.baseline
            _draw_text(
                canvas,
                draw,
                hdr.name_x,
                baseline,
                line.text,
                fonts,
                GROUP_NAME_SIZE,
                theme.nickname_color,
            )

    if card.timestamp is not None:
        line = card.timestamp
        x = max(CARD_PAD_X, (card.width - line.width) / 2.0)
        _draw_text(
            canvas,
            draw,
            x,
            card.timestamp_y + line.baseline,
            line.text,
            fonts,
            TIME_FONT_SIZE,
            theme.nickname_color,
        )

    # 群标题 / 时间与消息之间的那条细线
    if card.divider_y > 0:
        draw.line(
            (
                _s(CARD_PAD_X),
                _s(card.divider_y),
                _s(card.width - CARD_PAD_X),
                _s(card.divider_y),
            ),
            fill=_hex(DIVIDER_COLOR),
            width=max(1, int(round(_s(1)))),
        )

    for sep in card.separators:
        x = max(CARD_PAD_X, (card.width - sep.line.width) / 2.0)
        _draw_text(
            canvas,
            draw,
            x,
            sep.y + sep.line.baseline,
            sep.text,
            fonts,
            TIME_FONT_SIZE,
            theme.nickname_color,
        )

    for row in card.rows:
        # 头像
        avatar_size = int(_s(AVATAR_SIZE))
        draw.ellipse(
            (
                _s(row.avatar_x),
                _s(row.avatar_y),
                _s(row.avatar_x + AVATAR_SIZE),
                _s(row.avatar_y + AVATAR_SIZE),
            ),
            fill=_hex(AVATAR_BG),
        )
        if row.avatar is not None:
            avatar_img = _circle_image(row.avatar, avatar_size)
            canvas.paste(
                avatar_img, (int(_s(row.avatar_x)), int(_s(row.avatar_y))), avatar_img
            )

        # 头像挂件：不是每个适配器都给，有就叠在头像上
        if row.pendant is not None:
            pendant_size = int(_s(AVATAR_SIZE * PENDANT_SCALE))
            pendant_img = _fit_image(row.pendant, pendant_size)
            if pendant_img is not None:
                cx = _s(row.avatar_x + AVATAR_SIZE / 2.0)
                cy = _s(row.avatar_y + AVATAR_SIZE / 2.0)
                canvas.paste(
                    pendant_img,
                    (
                        int(round(cx - pendant_img.width / 2)),
                        int(round(cy - pendant_img.height / 2)),
                    ),
                    pendant_img,
                )

        # 昵称
        for index, line in enumerate(row.nickname.lines):
            baseline = row.nickname.y + line.baseline + index * NICKNAME_HEIGHT
            _draw_text(
                canvas,
                draw,
                row.nickname.x,
                baseline,
                line.text,
                fonts,
                NICKNAME_SIZE,
                theme.nickname_color,
            )

        # 互动标识图标，跟在昵称后面
        for honor in row.honors:
            if honor.image is not None:
                icon = _fit_image(honor.image, int(_s(HONOR_SIZE)))
                if icon is not None:
                    canvas.paste(
                        icon,
                        (
                            int(round(_s(honor.x) + (_s(HONOR_SIZE) - icon.width) / 2)),
                            int(round(_s(honor.y) + (_s(HONOR_SIZE) - icon.height) / 2)),
                        ),
                        icon,
                    )
                    continue
            draw.ellipse(
                (
                    _s(honor.x),
                    _s(honor.y),
                    _s(honor.x + HONOR_SIZE),
                    _s(honor.y + HONOR_SIZE),
                ),
                fill=_hex(honor.color),
            )
            glyph_font = fonts.font(HONOR_FONT_SIZE)
            try:
                g_ascent, g_descent = glyph_font.getmetrics()
            except Exception:
                g_ascent, g_descent = 0, 0
            baseline = (
                honor.y
                + (HONOR_SIZE - (g_ascent + g_descent) / OUTPUT_SCALE) / 2.0
                + g_ascent / OUTPUT_SCALE
            )
            try:
                glyph_width = float(glyph_font.getlength(honor.glyph)) / OUTPUT_SCALE
            except Exception:
                glyph_width = HONOR_FONT_SIZE * 0.6
            _draw_text(
                canvas,
                draw,
                honor.x + (HONOR_SIZE - glyph_width) / 2.0,
                baseline,
                honor.glyph,
                fonts,
                HONOR_FONT_SIZE,
                "#ffffff",
            )

        # 徽章：群头衔 / 群等级
        badge_font = fonts.font(BADGE_FONT_SIZE)
        try:
            b_ascent, b_descent = badge_font.getmetrics()
        except Exception:
            b_ascent, b_descent = 0, 0
        for badge in row.badges:
            draw.rounded_rectangle(
                (
                    _s(badge.x),
                    _s(badge.y),
                    _s(badge.x + badge.width),
                    _s(badge.y + badge.height),
                ),
                radius=int(_s(badge.height / 2.0)),
                fill=_hex(theme.badge_bg),
            )
            baseline = (
                badge.y
                + (badge.height - (b_ascent + b_descent) / OUTPUT_SCALE) / 2.0
                + b_ascent / OUTPUT_SCALE
            )
            _draw_text(
                canvas,
                draw,
                badge.x + BADGE_PAD_X,
                baseline,
                badge.text,
                fonts,
                BADGE_FONT_SIZE,
                theme.badge_color,
            )

        # 气泡
        draw.rounded_rectangle(
            (
                _s(row.bubble_x),
                _s(row.bubble_y),
                _s(row.bubble_x + row.bubble_w),
                _s(row.bubble_y + row.bubble_h),
            ),
            radius=int(_s(12)),
            fill=_hex(theme.bubble_bg),
        )
        if theme.tail:
            tail_y = row.bubble_y + min(row.bubble_h / 2.0, 20.0)
            draw.polygon(
                [
                    (_s(row.bubble_x + 1), _s(tail_y - 4.5)),
                    (_s(row.bubble_x - 5.5), _s(tail_y)),
                    (_s(row.bubble_x + 1), _s(tail_y + 4.5)),
                ],
                fill=_hex(theme.bubble_bg),
            )

        _draw_reply(canvas, draw, row.reply, fonts, skip_animation, theme)
        _draw_segments(
            canvas, draw, row.segments, theme.message_color, fonts, skip_animation
        )
        _draw_reactions(
            canvas, draw, row.reactions, fonts, theme, skip_animation
        )

    return canvas


# ------------------------------------------------------------------ 动图


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _lcm_capped(a: int, b: int, cap: int) -> int:
    if a <= 0 or b <= 0:
        return max(a, b, 1)
    value = a // _gcd(a, b) * b
    return cap if value > cap else value


def _frame_at(delays: list[int], elapsed: int) -> int:
    cycle = sum(delays)
    if cycle <= 0:
        return 0
    elapsed %= cycle
    for index, delay in enumerate(delays):
        if elapsed < delay:
            return index
        elapsed -= delay
    return len(delays) - 1


def _collect_animations(card: CardLayout) -> list[dict]:
    placements: list[dict] = []

    def add(segment: SegmentLayout) -> None:
        if segment.animation and segment.width > 0 and segment.height > 0:
            placements.append(
                {
                    "animation": segment.animation,
                    "kind": segment.kind,
                    "x": segment.x,
                    "y": segment.y,
                    "w": segment.width,
                    "h": segment.height,
                }
            )

    def walk_reply(reply: ReplyLayout | None) -> None:
        if reply is None:
            return
        walk_reply(reply.reply)
        for segment in reply.segments:
            add(segment)

    for row in card.rows:
        walk_reply(row.reply)
        for segment in row.segments:
            add(segment)
        for reaction in row.reactions:
            if reaction.animation:
                add(
                    SegmentLayout(
                        type="image",
                        kind="emoji",
                        x=reaction.x + REACTION_PAD_X,
                        y=reaction.y + (REACTION_HEIGHT - REACTION_EMOJI_SIZE) / 2.0,
                        width=REACTION_EMOJI_SIZE,
                        height=REACTION_EMOJI_SIZE,
                        animation=reaction.animation,
                    )
                )
    return placements


def _build_timeline(
    placements: list[dict],
    max_frames: int = MAX_GIF_FRAMES,
    max_duration: int = MAX_GIF_DURATION,
) -> list[dict]:
    if not placements:
        return [{"state": [], "delay": 0}]
    period, tick = 1, 0
    for placement in placements:
        cycle = 0
        for _frame, delay in placement["animation"]:
            cycle += delay
            tick = delay if tick <= 0 else _gcd(tick, delay)
        period = _lcm_capped(period, cycle, max_duration)
    period = min(period, max_duration)
    if tick <= 0:
        tick = MIN_FRAME_DELAY
    if (period + tick - 1) // tick > max_frames:
        tick = (period + max_frames - 1) // max_frames

    moments: list[dict] = []
    for elapsed in range(0, period, tick):
        state = [
            _frame_at([delay for _frame, delay in placement["animation"]], elapsed)
            for placement in placements
        ]
        delay = min(tick, period - elapsed)
        if moments and moments[-1]["state"] == state:
            moments[-1]["delay"] += delay
        else:
            moments.append({"state": state, "delay": delay})
    return moments


def _prepare_frames(placements: list[dict]) -> None:
    for placement in placements:
        size = (
            max(1, int(round(_s(placement["w"])))),
            max(1, int(round(_s(placement["h"])))),
        )
        frames = []
        for frame, _delay in placement["animation"]:
            img = frame.convert("RGBA").resize(size, Image.LANCZOS)
            if placement["kind"] != "emoji" and ImageChops is not None:
                mask = Image.new("L", size, 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, size[0] - 1, size[1] - 1),
                    radius=max(1, int(_s(IMAGE_RADIUS))),
                    fill=255,
                )
                img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
            frames.append(img)
        placement["frames"] = frames
        placement["pos"] = (
            int(round(_s(placement["x"]))),
            int(round(_s(placement["y"]))),
        )


def render_gif_sync(
    base_canvas: Any, placements: list[dict], out_path: str, bg: str = CARD_BG
) -> bool:
    """把动图段逐帧贴回卡片存成 GIF，没有多帧就返回 False。"""
    if Image is None or not placements:
        return False
    moments = _build_timeline(placements)
    if len(moments) <= 1:
        return False
    _prepare_frames(placements)

    frames = []
    delays = []
    for moment in moments:
        frame = base_canvas.copy()
        for placement, index in zip(placements, moment["state"]):
            images = placement["frames"]
            if not images:
                continue
            img = images[index % len(images)]
            frame.paste(img, placement["pos"], img)
        frames.append(frame)
        delays.append(max(20, int(moment["delay"]) * 10))

    if len(frames) < 2:
        return False
    # QQ 对动图是固定显示宽度：源图太大内容会被缩没，太大也不行
    w0, h0 = frames[0].size
    target = int(min(float(GIF_EXPORT_WIDTH), w0))
    if 0 < target < w0:
        size = (target, max(1, int(round(h0 * target / w0))))
        frames = [f.resize(size, Image.LANCZOS) for f in frames]
    # 透明圆角转 GIF 会变成黑边，先铺一层底色
    frames = [_flatten(f, bg) for f in frames]
    # 缩过 + 调色板量化会让字发糊，补一点锐化
    frames = [
        f.filter(ImageFilter.UnsharpMask(radius=1.0, percent=65, threshold=2))
        for f in frames
    ]

    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=delays,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return True


def _count_image_placeholders(card: CardLayout) -> int:
    """数一数这次有几处图片没取到、退化成了 "[图片]" 文本。"""
    total = 0

    def scan(layouts: list[SegmentLayout]) -> None:
        nonlocal total
        for item in layouts:
            if item.type != "text":
                continue
            for line in item.lines:
                if line.text == "[图片]":
                    total += 1

    def walk_reply(reply: ReplyLayout | None) -> None:
        if reply is None:
            return
        walk_reply(reply.reply)
        scan(reply.segments)

    for row in card.rows:
        walk_reply(row.reply)
        scan(row.segments)
    return total


async def render_quote_card(
    messages: Sequence[QuoteMessage],
    *,
    out_dir: str,
    filename: str = "quote",
    font_path: str = "",
    proxy: str = "",
    avatar_dir: str = "",
    animated: bool = True,
    image_resolver: Any = None,
    theme: "CardTheme | None" = None,
    group: dict | None = None,
    timestamp: str = "",
) -> str:
    """渲染引用图，返回文件路径（有动图就是 .gif，否则 .png）。"""
    if Image is None:
        raise RuntimeError("缺少 Pillow 依赖，无法生成引用图")

    theme = _contrast_theme(theme or DEFAULT_THEME)
    set_emoji_style(theme.emoji_style)
    resolved_font = resolve_font_path(font_path)
    if not resolved_font:
        logger.warning("[reply_guard] 没找到中文字体，引用图可能显示异常")

    loader = ResourceLoader(proxy=proxy, cache_dir=avatar_dir)
    prepared = await prepare_messages(loader, messages, image_resolver)
    group_header = dict(group) if group else None
    if group_header and group_header.get("avatar"):
        try:
            group_header["avatar"] = (await loader.load(
                str(group_header["avatar"])
            )).image
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[reply_guard] 取群头像失败: {e}")
            group_header["avatar"] = None
    engine = LayoutEngine(FontManager(resolved_font))
    card = engine.layout(prepared, group_header, timestamp)

    placeholders = _count_image_placeholders(card)
    if placeholders:
        logger.info(
            f"[reply_guard] 这次有 {placeholders} 处图片没取到，已退回「[图片]」占位"
        )

    placements = _collect_animations(card) if animated else []
    canvas = render_card_sync(
        card, resolved_font, skip_animation=bool(placements), theme=theme
    )

    os.makedirs(out_dir, exist_ok=True)
    if placements:
        gif_path = os.path.join(out_dir, f"{filename}.gif")
        try:
            if render_gif_sync(canvas, placements, gif_path, theme.card_bg):
                return gif_path
        except Exception as e:
            logger.warning(f"[reply_guard] 生成动图失败，退回静态图: {e}")

    png_path = os.path.join(out_dir, f"{filename}.png")
    _flatten(canvas, theme.card_bg).save(png_path, "PNG")
    return png_path
