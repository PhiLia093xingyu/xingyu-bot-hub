"""图源客户端：pixiv / yande.re / konachan。

只依赖 httpx，全部同步实现，由上层放进线程池调用。
所有请求都可以走 HTTP/SOCKS 代理（默认 127.0.0.1:7890）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

logger = logging.getLogger("astrbot")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PIXIV_REFERER = "https://www.pixiv.net/"

# pixiv 缩略图 -> 540px 预览图
_PIXIV_SIZE_RE = re.compile(r"/c/[^/]+/")
_PIXIV_SQUARE_RE = re.compile(r"_square1200\.jpg$")

# 中文 -> booru 标签（只收 yande.re / konachan 上确实存在、且有量的标签；
# 不存在的标签会在请求前被 tag.json 过滤掉）
CN_TAGS = {
    "猫耳": "nekomimi",
    "猫娘": "nekomimi",
    "兽耳": "animal_ears",
    "制服": "seifuku",
    "校服": "seifuku",
    "和服": "kimono",
    "浴衣": "yukata",
    "睡衣": "pajama",
    "马尾": "ponytail",
    "壁纸": "wallpaper",
    "横图": "landscape",
    "风景": "landscape",
}


# 中文 -> 日文 pixiv 标签（pixiv 上日文标签的命中率和质量都明显高于中文）
ZH_JA = {
    "猫耳": "猫耳",
    "猫娘": "猫耳",
    "兽耳": "獣耳",
    "狐耳": "狐耳",
    "兔耳": "うさ耳",
    "白发": "白髪",
    "银发": "銀髪",
    "金发": "金髪",
    "橙发": "オレンジ髪",
    "红发": "赤髪",
    "粉发": "ピンク髪",
    "蓝发": "青髪",
    "黑发": "黒髪",
    "棕发": "茶髪",
    "双马尾": "ツインテール",
    "马尾": "ポニーテール",
    "短发": "ショートヘア",
    "长发": "ロングヘア",
    "女仆": "メイド",
    "泳装": "水着",
    "泳衣": "水着",
    "和服": "着物",
    "浴衣": "浴衣",
    "制服": "制服",
    "校服": "制服",
    "水手服": "セーラー服",
    "旗袍": "チャイナドレス",
    "巫女": "巫女",
    "婚纱": "ウェディングドレス",
    "眼镜": "眼鏡",
    "微笑": "笑顔",
    "笑容": "笑顔",
    "睡衣": "パジャマ",
    "睡裙": "ネグリジェ",
    "过膝袜": "ニーソックス",
    "白丝": "白ニーソ",
    "黑丝": "黒ニーソ",
    "壁纸": "壁紙",
    "风景": "風景",
    "天空": "空",
    "云": "雲",
    "月亮": "月",
    "星星": "星",
    "樱花": "桜",
    "花": "花",
    "海": "海",
    "森林": "森",
    "雨": "雨",
    "雪": "雪",
    "夜景": "夜景",
    "黄昏": "夕焼け",
    "夏天": "夏",
    "冬天": "冬",
    "秋天": "秋",
    "春天": "春",
    "睡觉": "寝る",
    "侧脸": "横顔",
    "害羞": "照れ",
    "脸红": "赤面",
    "猫": "猫",
    "狗": "犬",
    "兔子": "うさぎ",
    "狐狸": "狐",
    "狼": "狼",
    "翅膀": "翼",
    "天使": "天使",
    "恶魔": "悪魔",
    "精灵": "エルフ",
    "魔法少女": "魔法少女",
    "女高中生": "女子高生",
    "少女": "少女",
    "姐姐": "お姉さん",
    "萝莉": "ロリ",
    "兔女郎": "バニーガール",
    "护士": "看護師",
}


def to_ja_tags(keyword: str) -> str:
    """把中文关键词换成日文标签；没收录的词原样保留。"""
    tokens = [t for t in re.split(r"[\s,，、]+", (keyword or "").strip()) if t]
    if not tokens:
        return ""
    mapped = [ZH_JA.get(t, t) for t in tokens]
    return " ".join(mapped)


class SourceError(RuntimeError):
    """图源请求失败（网络 / 代理 / 解析）。"""


@dataclass
class Pic:
    source: str
    title: str
    page_url: str
    image_url: str
    thumb_url: str = ""
    tags: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    rating: str = ""
    id: str = ""
    referer: str = ""
    local_path: str = ""
    use_proxy: bool = True

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "id": self.id,
            "title": self.title,
            "page": self.page_url,
            "image": self.image_url,
            "tags": self.tags,
            "size": f"{self.width}x{self.height}" if self.width else "",
            "rating": self.rating,
            "file": self.local_path,
        }


def _client(proxy: str = "", timeout: float = 20.0, verify: bool = True) -> httpx.Client:
    kwargs: dict = {
        "timeout": timeout,
        "verify": verify,
        "follow_redirects": True,
        # 代理线路偶发半死连接，主动断开可以少踩池化的坑
        "headers": {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "close",
        },
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def _get(client: httpx.Client, url: str, headers: dict | None = None, retries: int = 4) -> httpx.Response:
    import random

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, headers=headers)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise SourceError(f"HTTP {resp.status_code}")
            return resp
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(min(0.5 * (2**attempt), 2.0) + random.uniform(0, 0.3))
    raise SourceError(f"{type(last).__name__}: {last}") from last


# ---------------------------------------------------------------- pixiv


def _pixiv_preview(thumb_url: str) -> str:
    """把 250x250 的方形缩略图换成 540 宽的 master 预览图。"""
    if not thumb_url:
        return ""
    out = _PIXIV_SIZE_RE.sub("/c/540x540_70/", thumb_url)
    out = _PIXIV_SQUARE_RE.sub("_master1200.jpg", out)
    return out


def search_pixiv(
    keyword: str,
    limit: int = 5,
    proxy: str = "",
    cookie: str = "",
    safe: bool = True,
    timeout: float = 20.0,
    verify: bool = True,
    order: str = "popular_d",
    exclude_ai: bool = True,
) -> list[Pic]:
    """pixiv 关键词搜索（走 /ajax/search/artworks）。

    order: date_d=按新、popular_d=按收藏热度
    exclude_ai: 过滤掉 AI 生成作品（aiType == 2）
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise SourceError("关键词为空")

    headers = {
        "Referer": PIXIV_REFERER,
        "Accept": "application/json, text/plain, */*",
        "x-user-id": "",
    }
    cookies = {}
    phpsessid = (cookie or "").strip()
    if phpsessid:
        if "=" in phpsessid:  # 允许直接粘整段 cookie
            for part in phpsessid.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
        else:
            cookies["PHPSESSID"] = phpsessid
        headers["x-user-id"] = cookies.get("x-user-id", "")

    results: list[Pic] = []
    seen: set[str] = set()
    with _client(proxy, timeout, verify) as client:
        for page in range(1, 4):
            url = (
                "https://www.pixiv.net/ajax/search/artworks/"
                + quote(keyword, safe="")
                + f"?word={quote(keyword, safe='')}&order={order or 'popular_d'}&mode=all&p={page}"
                "&s_mode=s_tag_full&type=all&lang=zh"
            )
            resp = _get(client, url, headers=headers, retries=2)
            if resp.status_code != 200:
                raise SourceError(f"pixiv HTTP {resp.status_code}")
            try:
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise SourceError(f"pixiv 返回不是 JSON: {exc}") from exc
            if payload.get("error"):
                raise SourceError(f"pixiv: {payload.get('message') or '请求被拒绝'}")

            body = payload.get("body") or {}
            data = ((body.get("illustManga") or {}).get("data")) or []
            for item in data:
                pid = str(item.get("id") or "")
                if not pid or pid in seen:
                    continue
                if safe and int(item.get("xRestrict") or 0) > 0:
                    continue
                if exclude_ai and int(item.get("aiType") or 0) == 2:
                    continue
                seen.add(pid)
                thumb = str(item.get("url") or "")
                results.append(
                    Pic(
                        source="pixiv",
                        id=pid,
                        title=str(item.get("title") or ""),
                        page_url=f"https://www.pixiv.net/artworks/{pid}",
                        image_url=_pixiv_preview(thumb) or thumb,
                        thumb_url=thumb,
                        tags=[str(t) for t in (item.get("tags") or [])][:12],
                        width=int(item.get("width") or 0),
                        height=int(item.get("height") or 0),
                        rating="r18" if int(item.get("xRestrict") or 0) > 0 else "safe",
                        referer=PIXIV_REFERER,
                    )
                )
                if len(results) >= limit:
                    return results
            if not data:
                break
    return results


# ---------------------------------------------------------------- booru


def _booru_tags(keyword: str) -> list[str]:
    raw = (keyword or "").strip()
    if not raw:
        raise SourceError("关键词为空")
    terms: list[str] = []
    for token in re.split(r"[\s,，、]+", raw):
        if not token:
            continue
        terms.append(CN_TAGS.get(token, token.lower().replace(" ", "_")))
    return terms or [raw.lower().replace(" ", "_")]


def _existing_tags(client: httpx.Client, base: str, terms: list[str]) -> list[str]:
    """过滤掉该图源上不存在的标签（yande 的词表和 danbooru 不同）。"""
    keep: list[str] = []
    for term in terms:
        try:
            resp = client.get(f"{base.rstrip('/')}/tag.json", params={"name": term, "limit": 20})
            data = resp.json()
            if any(
                d.get("name") == term and int(d.get("count") or 0) > 0
                for d in (data or [])
            ):
                keep.append(term)
        except Exception:  # noqa: BLE001
            keep.append(term)  # 查不到就按原样带上，交给后面的搜索结果说话
    return keep


def _fetch_posts(client: httpx.Client, base: str, tags: str, limit: int) -> list[dict]:
    url = f"{base.rstrip('/')}/post.json?tags={tags}&limit={max(1, min(limit, 20))}"
    resp = _get(client, url, retries=2)
    if resp.status_code != 200:
        raise SourceError(f"{base} HTTP {resp.status_code}")
    try:
        posts = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise SourceError(f"{base} 返回不是 JSON: {exc}") from exc
    return posts or []


def search_booru(
    base: str,
    keyword: str,
    limit: int = 5,
    proxy: str = "",
    safe: bool = True,
    timeout: float = 20.0,
    verify: bool = True,
) -> list[Pic]:
    """yande.re / konachan 系（Moebooru）搜索。"""
    terms = _booru_tags(keyword)
    rating = "+rating%3Asafe" if safe else ""
    with _client(proxy, timeout, verify) as client:
        keep = _existing_tags(client, base, terms)
        if not keep:
            return []
        posts = _fetch_posts(client, base, "+".join(keep) + rating, limit)
        if not posts and len(keep) > 1:
            # 标签叠太多没结果时，逐个放宽
            for term in keep:
                posts = _fetch_posts(client, base, term + rating, limit)
                if posts:
                    break

    out: list[Pic] = []
    for post in posts:
        image_url = post.get("sample_url") or post.get("file_url") or post.get("jpeg_url")
        if not image_url:
            continue
        out.append(
            Pic(
                source=base.split("//")[-1].split(".")[0],
                id=str(post.get("id") or ""),
                title=" ".join(str(post.get("tags") or "").split()[:6]),
                page_url=f"{base.rstrip('/')}/post/show/{post.get('id')}",
                image_url=image_url,
                thumb_url=post.get("preview_url") or "",
                tags=[t for t in str(post.get("tags") or "").split() if t][:12],
                width=int(post.get("sample_width") or post.get("width") or 0),
                height=int(post.get("sample_height") or post.get("height") or 0),
                rating=str(post.get("rating") or ""),
                referer=base.rstrip("/") + "/",
            )
        )
    return out


# ---------------------------------------------------------------- bing（国内可达的兜底源）


def search_bing(
    keyword: str,
    limit: int = 5,
    timeout: float = 20.0,
    verify: bool = True,
) -> list[Pic]:
    """Bing 图片搜索，直连（不走代理）。代理挂了的时候靠它兜底。"""
    from urllib.parse import quote

    keyword = (keyword or "").strip()
    if not keyword:
        raise SourceError("关键词为空")
    url = (
        f"https://cn.bing.com/images/async?q={quote(keyword)}&first=0&count=35"
        "&mmasync=1&dgst=0&SFX=1&lostate=r&IG=0&IID=images.1"
    )
    headers = {"User-Agent": UA, "Referer": "https://cn.bing.com/images/", "Connection": "close"}
    with httpx.Client(timeout=timeout, verify=verify, headers=headers, follow_redirects=True) as client:
        resp = _get(client, url, retries=2)
        html = resp.text

    urls = []
    for raw in re.findall(r"murl&quot;:&quot;(.*?)&quot;", html):
        u = raw.replace("\\/", "/").strip()
        if u.startswith("http") and u not in urls:
            urls.append(u)
    if not urls:
        raise SourceError("bing 没解析到图片")

    out: list[Pic] = []
    for i, u in enumerate(urls[: max(limit * 3, 10)]):
        out.append(
            Pic(
                source="bing",
                id=f"{i}-{abs(hash(u)) % 10**8}",
                title=keyword,
                page_url=f"https://cn.bing.com/images/search?q={quote(keyword)}",
                image_url=u,
                referer="https://cn.bing.com/",
                use_proxy=False,
            )
        )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- download


def download_image(pic: Pic, dest_dir: str, proxy: str = "", timeout: float = 30.0, verify: bool = True) -> str:
    """把一张图下载到 dest_dir，返回本地路径。"""
    import os
    import uuid

    os.makedirs(dest_dir, exist_ok=True)
    headers = {"Referer": pic.referer or "https://www.pixiv.net/"}
    with _client(proxy, timeout, verify) as client:
        resp = _get(client, pic.image_url, headers=headers, retries=2)
        if resp.status_code != 200:
            raise SourceError(f"下载失败 HTTP {resp.status_code}")
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(ctype, ".jpg")
        path = os.path.join(dest_dir, f"{pic.source}_{pic.id or uuid.uuid4().hex[:8]}{ext}")
        with open(path, "wb") as fh:
            fh.write(resp.content)
    pic.local_path = path
    return path
