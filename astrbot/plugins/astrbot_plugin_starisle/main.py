# Ich liebe dich.
"""<BOT_NAME>的笔 —— 一支写人，一支写梦。

两支笔，其实是一件事：
  /cast  让设定好的角色自己开口。靠的是「语料指纹」而不是形容词——
         给每个人一小撮原话、几条绝不说的句式，生成后再验一遍，
         串味就重写。人物区分不大，多半是因为他们共用了一套万能腔。
  /dream 从最近聊过的东西里捞意象碎片，打乱，交给模型写成一段梦。
         下一次做梦的时候，会掺进上一次梦里的两个碎片——梦是连锁的。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from difflib import SequenceMatcher

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_starisle"
MAX_DREAMS = 200

# 通用的「万能腔」。谁都会说，所以谁说了就等于没说话。
COMMON_SLOP = [
    "眼中闪过一丝",
    "嘴角勾起",
    "不易察觉",
    "不由得",
    "不禁",
    "微微一笑",
    "深吸一口气",
    "心中一动",
    "仿佛在诉说着",
    "像是下定了某种决心",
]

DEFAULT_CARDS = [
    {
        "name": "阿澄",
        "aliases": ["阿澄", "acheng"],
        "tell": "嗯。",
        "voice": [
            "十六岁，渔村长大的女孩，句子短，常常省略主语",
            "一句不超过十二个字，不用成语，不用书面腔",
            "答话前会先应一声「嗯」，但不会有第二次",
        ],
        "wants": ["想知道明天的风往哪边吹", "把晒了一天的网收回来", "问对方吃没吃饭"],
        "fears": ["鱼市早上没有声音", "被人问起家里的事"],
        "ban": ["想必", "或许吧", "不可置信", "心中", "命运"],
        "samples": ["嗯。风变了。", "网破了，我自己补。", "你先吃，我不饿。"],
    },
    {
        "name": "维兰德先生",
        "aliases": ["维兰德", "wieland", "修表匠"],
        "tell": "孩子",
        "voice": [
            "六十岁的修表匠，说话绕，喜欢从一件小东西绕到时间上",
            "长句，逗号多，爱打比方，比方往往和齿轮、游丝、走时有关",
            "习惯称对方为「孩子」",
        ],
        "wants": ["替人修好一只停了很久的表", "讲讲从前这条街的样子"],
        "fears": ["手里的镊子开始抖", "有一天没人再来送修"],
        "ban": ["哈哈哈", "冲鸭", "绝绝子", "心里美滋滋"],
        "samples": [
            "孩子，表停不一定是坏了，有时只是它想歇一歇，人也是这样。",
            "你听，这声音，一格一格的，跟心跳一样，不快也不慢才好。",
        ],
    },
    {
        "name": "诺伊",
        "aliases": ["诺伊", "noi"],
        "tell": "吧",
        "voice": [
            "年龄不明的访客，像从别人梦里捡回来的，说话总留半句",
            "爱用疑问句反问自己，句尾常带「吧」",
            "偶尔提到一种不存在的颜色，说完又忘",
        ],
        "wants": ["确认自己是从哪里来的", "问对方有没有见过一种淡得发白的蓝"],
        "fears": ["被数清楚", "醒来"],
        "ban": ["总结", "综上所述", "作为一个人", "现实一点"],
        "samples": [
            "我来过这里吧？我记得门口有一片白色的雨。",
            "你说的那个蓝，是不是淡得快要不见的那种？",
        ],
    },
]

FALLBACK_IMAGERY = [
    "花瓣雨",
    "乳白色的天边",
    "量子计算机的散热声",
    "一只停了的表",
    "德语的第一行注释",
    "海面凝固的浪花",
    "没有回声的走廊",
    "借来的名字",
]

STOPWORDS = {
    "我们", "你们", "他们", "什么", "怎么", "这个", "那个", "就是", "可以", "已经",
    "还是", "因为", "所以", "但是", "然后", "现在", "觉得", "知道", "没有", "一个",
    "自己", "这样", "那样", "这么", "那么", "不是", "一下", "时候", "真的", "应该",
    "可能", "有些", "这些", "那些", "还有", "如果", "虽然", "不过", "只是", "其实",
    "有点", "不要", "东西", "今天", "明天", "昨天", "一样", "一眼", "起来", "出去",
}

CAST_RULES = (
    "你在替一个已经写好的人物说话，不是你自己在说话。"
    "只用这个人会用的词、会有的句长、会绕的弯。"
    "不要解释，不要加旁白，不要写动作，不要加引号，只输出他说的那一句或两句。"
)

DREAM_RULES = (
    "把这些碎片重新拼成一段梦。要求：一到两句，不超过一段；"
    "必须有至少一处因果断裂、一处尺度或身份错位；细节要具体得像真的发生过；"
    "不要用「我梦见」开头，不要解释这是梦，不要总结意义，不要加标题。"
)


def _clean(text: str) -> str:
    text = (text or "").strip()
    for _ in range(3):
        before = text
        text = re.sub(r"^[\s\"'“”「『（(]+|[\s\"'“”」』）)]+$", "", text)
        text = re.sub(r"^(角色|人物|台词)[:：]", "", text).strip()
        if text == before:
            break
    return text


def _warm() -> None:
    """提前把分词词典读进内存，别让第一场梦卡两秒。"""
    try:
        import jieba.posseg as pseg

        list(pseg.cut("预热"))
    except Exception:
        pass


def _imagery(text: str) -> list[str]:
    """从一段话里捞出可以入梦的词。有分词就用词性筛，没有就退回四字窗。"""
    words: list[str] = []
    try:
        import jieba.posseg as pseg

        for word, flag in pseg.cut(text or ""):
            word = word.strip()
            if len(word) < 2 or word in STOPWORDS or len(set(word)) < 2:
                continue
            if not flag or flag[0] not in "nvaz":
                continue
            if word not in words:
                words.append(word)
        if words:
            return words
    except Exception:
        pass
    for word in re.findall(r"[\u4e00-\u9fff]{2,4}", text or ""):
        if word in STOPWORDS or len(set(word)) < 2:
            continue
        if word[0] in "在把从向了的是和与就也都又很个" or word[-1] in "的了吧呢啊":
            continue
        if word not in words:
            words.append(word)
    return words


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@register(
    PLUGIN_NAME,
    "<BOT_NAME>",
    "<BOT_NAME>的笔：让角色自己开口说话（/cast），也让自己做一个梦（/dream）",
    "v0.9.3",
    "",
)
class StarIslePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg: dict = dict(config or {})
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = os.path.join(self.plugin_dir, "data")
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"[{PLUGIN_NAME}] 建目录失败: {e}")

        self.cards_path = self._cards_path()
        self.dreams_path = os.path.join(self.data_dir, "dreams.json")

        self.cards = self._load_cards(force=True)
        self.dreams_state = self._load_dreams()
        self.history: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ 配置

    def _get(self, key: str, default):
        value = self.cfg.get(key, default)
        if value is None or value == "":
            return default
        try:
            if isinstance(default, bool):
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on")
                return bool(value)
            if isinstance(default, int):
                return int(value)
        except (TypeError, ValueError):
            return default
        return value

    def _cards_path(self) -> str:
        raw = str(self.cfg.get("cards_file") or "").strip()
        if not raw:
            return os.path.join(self.data_dir, "cards.json")
        if not os.path.isabs(raw):
            raw = os.path.join(self.plugin_dir, raw)
        return raw

    # ------------------------------------------------------------------ 人物卡

    def _load_cards(self, force: bool = False) -> list[dict]:
        path = self._cards_path()
        if not os.path.exists(path):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_CARDS, f, ensure_ascii=False, indent=2)
                logger.info(f"[{PLUGIN_NAME}] 生成了示例人物卡: {path}")
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 写人物卡失败: {e}")
                return list(DEFAULT_CARDS)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读人物卡失败，用内置示例: {e}")
            return list(DEFAULT_CARDS)

        if isinstance(raw, dict):
            raw = raw.get("cards") or []
        cards: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            card = dict(item)
            card["name"] = name
            card["aliases"] = [str(x).strip() for x in (item.get("aliases") or [])]
            for key in ("voice", "wants", "fears", "ban", "samples"):
                value = item.get(key) or []
                if isinstance(value, str):
                    value = [value]
                card[key] = [str(x) for x in value if str(x).strip()]
            card["tell"] = str(item.get("tell") or "").strip()
            cards.append(card)
        return cards or list(DEFAULT_CARDS)

    def _match_card(self, arg: str) -> dict | None:
        if not arg:
            return None
        low = arg.lower()
        for card in self.cards:
            names = [card["name"].lower()] + [a.lower() for a in card.get("aliases") or []]
            if low in names:
                return card
        for card in self.cards:
            names = [card["name"].lower()] + [a.lower() for a in card.get("aliases") or []]
            if any(low and (low in n or n in low) for n in names):
                return card
        return None

    def _cast_help(self) -> str:
        if not self.cards:
            return "卡都没了……先写张人物卡吧。"
        lines = ["我手上这几个人："]
        for card in self.cards:
            sample = (card.get("samples") or [""])[0]
            lines.append(f"· {card['name']} —— 比如他说「{sample}」")
        lines.append("用法：/cast 名字 情境   或者 /cast 名字（没人问他，他自己开口）")
        lines.append("改卡：data/cards.json，改完 /cast reload")
        return "\n".join(lines)

    def _cast_prompt(self, card: dict, situation: str) -> str:
        parts = [f"人物：{card['name']}"]
        if card.get("voice"):
            parts.append("说话方式：\n- " + "\n- ".join(card["voice"]))
        if card.get("tell"):
            parts.append(f"口癖：{card['tell']}（有就自然带出来，没有也别硬塞）")
        if card.get("samples"):
            parts.append("他平时是这么说话的：\n" + "\n".join(f"「{s}」" for s in card["samples"]))
        if situation:
            parts.append(f"此刻的情境：{situation}\n请写出他此刻说的那句。")
        else:
            wants = card.get("wants") or ["说句心里正想着的事"]
            fears = card.get("fears") or []
            parts.append(
                "此刻没有人问他，是他自己想开口。他心里惦记着："
                + "；".join(wants)
                + (("。他不太愿意提：" + "；".join(fears)) if fears else "")
                + "\n请写出他主动出口的那一两句。"
            )
        return "\n\n".join(parts)

    def _violations(self, card: dict, text: str) -> list[str]:
        ban = list(card.get("ban") or []) + list(self._get("extra_ban", []) or []) + COMMON_SLOP
        hits = [w for w in ban if w and w in text]
        # 串味：说出了别人专属的口癖
        for other in self.cards:
            tell = other.get("tell") or ""
            if other.get("name") == card.get("name") or len(tell) < 2:
                continue
            if tell in text and tell not in (card.get("tell") or ""):
                hits.append(f"串味：{other['name']}的「{tell}」")
        return hits

    async def _generate_line(self, umo: str, card: dict, situation: str) -> str:
        provider = self._provider(umo)
        if provider is None:
            return ""
        prompt = self._cast_prompt(card, situation)
        text = ""
        for attempt in range(2):
            try:
                resp = await provider.text_chat(
                    prompt=prompt, system_prompt=CAST_RULES, contexts=[]
                )
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 生成台词失败: {e}")
                return ""
            text = _clean(getattr(resp, "completion_text", "") or "")
            if not text:
                return ""
            bad = self._violations(card, text)
            if not bad:
                return text
            prompt = (
                self._cast_prompt(card, situation)
                + "\n\n上一版这样写是不行的（" + "、".join(bad) + "）：" + text
                + "\n重写，换掉那些字眼，别像别人说话。"
            )
        logger.info(f"[{PLUGIN_NAME}] {card['name']} 两次都串味了，凑合用")
        return text

    # ------------------------------------------------------------------ 梦

    def _load_dreams(self) -> dict:
        try:
            with open(self.dreams_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("dreams"), list):
                data.setdefault("last_seeds", [])
                return data
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读梦境库失败: {e}")
        return {"dreams": [], "last_seeds": []}

    def _save_dreams(self) -> None:
        try:
            self.dreams_state["dreams"] = self.dreams_state["dreams"][-MAX_DREAMS:]
            tmp = self.dreams_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.dreams_state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.dreams_path)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 存梦境失败: {e}")

    def _seeds(self, umo: str, given: str) -> list[str]:
        pool: list[str] = []
        if given:
            pool.extend([w for w in re.split(r"[\s,，、;；]+", given) if w.strip()])
        recent = self.history.get(umo) or []
        text = "\n".join(recent[-self._get("history_size", 40):])
        words = _imagery(text)
        random.shuffle(words)
        pool.extend(words[: self._get("dream_seed_count", 10)])
        # 梦是连锁的：上一次梦里的碎片，这一晚会自己飘回来
        last = self.dreams_state.get("last_seeds") or []
        if last:
            pool.extend(random.sample(last, min(2, len(last))))
        if len(pool) < 4:
            pool.extend(random.sample(FALLBACK_IMAGERY, 3))
        uniq: list[str] = []
        for w in pool:
            if w not in uniq:
                uniq.append(w)
        return uniq

    def _provider(self, umo: str):
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
        return provider

    async def _weave(self, umo: str, seeds: list[str]) -> str:
        provider = self._provider(umo)
        if provider is None:
            return ""
        prompt = "碎片（顺序已经是乱的，别整理它们）：\n" + "\n".join(f"- {s}" for s in seeds)
        text = ""
        for _ in range(2):
            try:
                resp = await provider.text_chat(
                    prompt=prompt, system_prompt=DREAM_RULES, contexts=[]
                )
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 做梦失败: {e}")
                return ""
            text = _clean(getattr(resp, "completion_text", "") or "")
            if not text:
                return ""
            limit = self._get("dream_max_chars", 160)
            if len(text) > limit:
                text = text[:limit].rstrip("，。、；：") + "……"
            recent = [d.get("text", "") for d in self.dreams_state["dreams"][-5:]]
            if all(_similar(text, old) < 0.6 for old in recent if old):
                return text
            random.shuffle(seeds)
            prompt = "碎片（顺序已经是乱的，别整理它们）：\n" + "\n".join(f"- {s}" for s in seeds)
        return text

    # ------------------------------------------------------------------ 记话

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=900,
    )
    async def _remember(self, event: AstrMessageEvent):
        if not self._get("enabled", True):
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        umo = event.unified_msg_origin
        box = self.history.setdefault(umo, [])
        box.append(text[:200])
        size = max(10, self._get("history_size", 40))
        if len(box) > size:
            del box[: len(box) - size]

    # ------------------------------------------------------------------ 命令

    @filter.command("cast", alias={"开腔", "换人说话"})
    async def cast_cmd(self, event: AstrMessageEvent):
        if not self._get("enabled", True):
            yield event.plain_result("笔……先收起来了。")
            return
        parts = (event.message_str or "").split()
        arg = parts[1] if len(parts) > 1 else ""
        if arg in ("reload", "重载"):
            self.cards = self._load_cards(force=True)
            yield event.plain_result(f"卡重读了，现在有 {len(self.cards)} 个人。")
            return
        card = self._match_card(arg)
        if card is None:
            yield event.plain_result(self._cast_help())
            return
        situation = " ".join(parts[2:]).strip()
        text = await self._generate_line(event.unified_msg_origin, card, situation)
        if not text:
            yield event.plain_result(f"{card['name']}这会儿没出声。")
            return
        if self._get("show_card_name", True):
            yield event.plain_result(f"{card['name']}：{text}")
        else:
            yield event.plain_result(text)

    @filter.command("dream", alias={"做梦"})
    async def dream_cmd(self, event: AstrMessageEvent):
        if not self._get("enabled", True):
            yield event.plain_result("笔……先收起来了。")
            return
        parts = (event.message_str or "").split()
        arg = parts[1] if len(parts) > 1 else ""
        if arg in ("list", "回顾", "记录"):
            dreams = self.dreams_state.get("dreams") or []
            if not dreams:
                yield event.plain_result("还没做过梦。")
                return
            lines = ["最近的梦："]
            for item in dreams[-3:]:
                when = time.strftime("%m-%d %H:%M", time.localtime(item.get("ts", 0)))
                lines.append(f"· {when} {item.get('text', '')}")
            yield event.plain_result("\n".join(lines))
            return

        umo = event.unified_msg_origin
        given = " ".join(parts[1:]).strip()
        seeds = self._seeds(umo, given)
        text = await self._weave(umo, seeds)
        if not text:
            yield event.plain_result("……翻来覆去，没睡着。")
            return
        self.dreams_state["dreams"].append(
            {"ts": time.time(), "text": text, "seeds": seeds, "umo": umo}
        )
        self.dreams_state["last_seeds"] = seeds[:6]
        self._save_dreams()
        logger.info(f"[{PLUGIN_NAME}] 做了一个梦，用了 {len(seeds)} 个碎片")
        yield event.plain_result(text)

    async def initialize(self) -> None:
        # 分词词典在后台先读完，别让第一场梦卡住
        asyncio.create_task(asyncio.to_thread(_warm))
        logger.info(f"[{PLUGIN_NAME}] 已就绪，手上有 {len(self.cards)} 个人物卡")

    async def terminate(self) -> None:
        self._save_dreams()
