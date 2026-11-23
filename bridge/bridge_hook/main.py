#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WeChat 4.1.10.27 -> OneBot v11 bridge built on the open-source WechatHook HTTP API.

Data flow:
    Weixin.exe + version.dll (hook)  --HTTP POST-->  this process (:30002/event)
    this process  --OneBot v11 reverse WS-->  AstrBot (:24875)
    AstrBot --send_*-->  this process  --POST /SendTextMsg-->  hook (:30001) --> WeChat

This replaces the UIA bridge for WeChat 4.1.10.27; the OneBot/AstrBot side is identical.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve

LOG = logging.getLogger("hookbridge")

MSG_TYPES = {
    1: "text", 3: "image", 34: "voice", 37: "friend_request", 42: "card", 43: "video",
    47: "emotion", 48: "location", 49: "link", 10000: "system", 10002: "recall",
}
PLACEHOLDER = {
    "image": "[图片]", "voice": "[语音]", "video": "[视频]", "link": "[链接]",
    "emotion": "[表情]", "card": "[名片]", "location": "[位置]", "system": "", "recall": "",
}


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    cfg.setdefault("hook", {})
    cfg["hook"].setdefault("api_base", "http://127.0.0.1:30001")
    cfg["hook"].setdefault("callback_host", "127.0.0.1")
    cfg["hook"].setdefault("callback_port", 30002)
    cfg["hook"].setdefault("callback_path", "/event")
    cfg.setdefault("onebot", {})
    cfg["onebot"].setdefault("self_id", 0)
    cfg["onebot"].setdefault("report_self_message", False)
    cfg.setdefault("logging", {"level": "INFO", "file": "logs/hookbridge.log"})
    return cfg


def setup_logging(cfg: dict) -> None:
    level = getattr(logging, str(cfg["logging"].get("level", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    lf = Path(cfg["logging"].get("file", "logs/hookbridge.log"))
    if not lf.is_absolute():
        lf = ROOT / lf
    lf.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(lf, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def stable_int(text: str) -> int:
    n = zlib.crc32(str(text).encode("utf-8")) & 0x7FFFFFFF
    return n if n >= 10000 else n + 10000


class IdMap:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: Dict[str, Any] = {"groups": {}, "users": {}, "self_id": 0}
        try:
            if path.exists():
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            LOG.warning("id map unreadable: %s", traceback.format_exc())

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def self_id(self) -> int:
        return int(self.data.get("self_id") or 0)

    def set_self_id(self, v: int) -> None:
        with self.lock:
            self.data["self_id"] = int(v)
            self.save()

    def group_id(self, room: str, name: str = "") -> int:
        digits = room.split("@")[0]
        gid = int(digits) if digits.isdigit() and len(digits) <= 18 else stable_int(room)
        with self.lock:
            e = self.data["groups"].setdefault(str(gid), {})
            e["chat_id"] = room
            if name:
                e["name"] = name
            self.save()
        return gid

    def user_id(self, wxid: str, name: str = "") -> int:
        uid = int(wxid) if str(wxid).isdigit() and len(str(wxid)) <= 18 else stable_int(wxid)
        with self.lock:
            e = self.data["users"].setdefault(str(uid), {})
            e["wxid"] = wxid
            if name:
                e["name"] = name
            self.save()
        return uid

    def lookup_group(self, gid: int) -> Optional[dict]:
        return self.data["groups"].get(str(gid))

    def lookup_user(self, uid: int) -> Optional[dict]:
        return self.data["users"].get(str(uid))

    def group_name(self, gid: int) -> str:
        e = self.lookup_group(gid) or {}
        return e.get("name") or e.get("chat_id") or str(gid)

    def user_name(self, uid: int) -> str:
        e = self.lookup_user(uid) or {}
        return e.get("name") or e.get("wxid") or str(uid)


class HookApi:
    """Thin client for the WechatHook HTTP API (127.0.0.1:30001)."""

    def __init__(self, base: str, image_root: str = "", self_wxid: str = ""):
        self.base = base.rstrip("/")
        self.contacts: Dict[str, dict] = {}
        self.self_wxid = self_wxid
        self.self_nick = ""
        self._contacts_ts = 0.0
        self.image_root = image_root or str(Path(os.path.expanduser("~")) / "Documents" / "xwechat_files")
        self._acct = None
        self._img_cache: Dict[str, str] = {}

    def _req(self, path: str, payload: Optional[dict] = None, timeout: float = 6.0) -> Tuple[int, Any]:
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return e.code, {}
        except Exception as e:
            return 0, {"error": repr(e)}

    def alive(self) -> bool:
        code, _ = self._req("/QueryDB/status", timeout=3)
        return code == 200

    def profile(self) -> dict:
        code, data = self._req("/GetSelfProfile", {})
        return data if code == 200 and isinstance(data, dict) else {}

    def refresh_contacts(self, force: bool = False) -> None:
        if not force and time.time() - self._contacts_ts < 300 and self.contacts:
            return
        code, data = self._req("/GetContacts", {})
        if code != 200 or not isinstance(data, dict):
            return
        items = data.get("contacts") or []
        if not data.get("complete", True) and not items:
            return
        for c in items:
            wxid = c.get("username") or c.get("UserName") or ""
            if not wxid:
                continue
            self.contacts[wxid] = c
        self._contacts_ts = time.time()

    def name_of(self, wxid: str) -> str:
        c = self.contacts.get(wxid) or {}
        return c.get("remark") or c.get("nick_name") or c.get("NickName") or wxid

    def _account_dir(self):
        if self._acct is not None:
            return self._acct
        root = Path(self.image_root)
        if not root.exists():
            return None
        for d in root.iterdir():
            try:
                if d.is_dir() and str(d.name).startswith(self.self_wxid or ""):
                    self._acct = d
                    return d
            except OSError:
                continue
        return None

    def decode_image(self, xml_text: str):
        """Decode a received image (.dat in WeChat's attach cache) to a local jpg/png."""
        mlen = re.search(r'(?<![A-Za-z])length="(\d+)"', xml_text or "")
        if not mlen:
            return None
        length = int(mlen.group(1))
        if length <= 0 or length > 8 * 1024 * 1024:
            return None
        mmd5 = re.search(r'md5="([0-9a-fA-F]{16,64})"', xml_text or "")
        md5 = mmd5.group(1).lower() if mmd5 else ""
        if md5 and md5 in self._img_cache:
            return self._img_cache[md5]
        acct = self._account_dir()
        if acct is None:
            return None
        outdir = ROOT / "state" / "images"
        outdir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        cands = []
        try:
            for p in acct.glob("msg/attach/*/*/Img/*.dat"):
                name = p.name
                if name.endswith("_t.dat") or name.endswith("_h.dat"):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if now - st.st_mtime > 600:
                    continue
                if abs(st.st_size - length) > 96:
                    continue
                cands.append((st.st_mtime, p))
        except Exception:
            return None
        for _, p in sorted(cands, reverse=True)[:5]:
            out = outdir / ((md5 or p.stem) + ".jpg")
            code, _resp = self._req("/Decode_Pic", {"src_path": str(p), "dst_path": str(out)}, timeout=20)
            if code != 200:
                continue
            try:
                data = out.read_bytes()
            except OSError:
                continue
            if len(data) != length:
                continue
            if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
                if md5:
                    self._img_cache[md5] = str(out)
                return str(out)
        return None


    def _wechat_pid(self):
        try:
            pid_file = ROOT / "state" / "wechat.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text(encoding="ascii").strip())
                import psutil
                if pid > 0 and psutil.pid_exists(pid):
                    return pid
        except Exception:
            pass
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                if (proc.info.get("name") or "").lower() == "weixin.exe":
                    return proc.pid
        except Exception:
            pass
        return None

    def _frida_scan(self):
        import frida
        pid = self._wechat_pid()
        if not pid:
            return None
        sess = getattr(self, "_frida_session", None)
        if sess is None:
            sess = frida.attach(pid)
            script = sess.create_script(VOICE_SCAN_JS)
            script.load()
            self._frida_session = sess
            self._frida_script = script
        return self._frida_script

    def capture_voice(self, voicelength_ms: int, voicemd5: str = ""):
        """Find the received SILK buffer in WeChat memory, decode to wav."""
        try:
            import pilk, wave
        except Exception:
            return None
        try:
            script = self._frida_scan()
            if script is None:
                return None
            hits = script.exports_sync.scan()
            if not hits:
                return None
            target = max(0.5, (voicelength_ms or 0) / 1000.0)
            outdir = ROOT / "state" / "voices"
            outdir.mkdir(parents=True, exist_ok=True)
            best = None
            for addr in hits:
                hx = script.exports_sync.dumphex(addr, 16384)
                if not isinstance(hx, str) or hx.startswith("ERR:") or len(hx) < 64:
                    continue
                data = bytes.fromhex(hx)
                exact = silk_exact(data)
                if not exact or len(exact) < 24:
                    continue
                if voicemd5 and hashlib.md5(exact).hexdigest() == voicemd5:
                    best = exact
                    break
                silk_path = outdir / "tmp.silk"
                wav_path = outdir / "tmp.wav"
                silk_path.write_bytes(exact)
                try:
                    pilk.silk_to_wav(str(silk_path), str(wav_path), 24000)
                    with wave.open(str(wav_path)) as w:
                        dur = w.getnframes() / float(w.getframerate())
                except Exception:
                    continue
                score = abs(dur - target)
                if best is None or score < best[0]:
                    best = (score, exact, dur)
            if not best:
                return None
            if isinstance(best, bytes):
                exact = best
            else:
                score, exact, dur = best
                if score > 0.45:
                    return None
            wav_path = outdir / (hashlib.md5(exact).hexdigest() + ".wav")
            silk_path = outdir / (hashlib.md5(exact).hexdigest() + ".silk")
            silk_path.write_bytes(exact)
            pilk.silk_to_wav(str(silk_path), str(wav_path), 24000)
            LOG.info("voice captured: %s (%d bytes)", wav_path.name, len(exact))
            return str(wav_path)
        except Exception as e:
            LOG.warning("voice capture failed: %r", e)
            return None

    def fetch_sticker(self, xml_text: str):
        """Download a custom sticker from WeChat CDN (plain gif/png) for vision."""
        m = re.search(r'cdnurl="([^"]+)"', xml_text or "")
        if not m:
            return None
        url = m.group(1).replace("&amp;", "&")
        md5m = re.search(r'md5="([0-9a-fA-F]{16,64})"', xml_text or "")
        md5 = md5m.group(1).lower() if md5m else hashlib.md5(url.encode()).hexdigest()
        outdir = ROOT / "state" / "stickers"
        outdir.mkdir(parents=True, exist_ok=True)
        raw = outdir / (md5 + ".bin")
        png = outdir / (md5 + ".png")
        if png.exists():
            return str(png)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read(8 * 1024 * 1024 + 1)
        except Exception as e:
            LOG.warning("sticker download failed: %r", e)
            return None
        if len(data) > 8 * 1024 * 1024 or len(data) < 32:
            return None
        raw.write_bytes(data)
        magic = data[:8]
        ext = ".gif" if magic[:3] == b"GIF" else (".png" if magic == b"\x89PNG\r\n\x1a\n" else (".jpg" if magic[:3] == b"\xff\xd8\xff" else ""))
        if not ext:
            return None
        try:
            from PIL import Image
            im = Image.open(raw)
            im.seek(0)
            frame = im.convert("RGB")
            frame.save(png, "PNG")
            return str(png)
        except Exception:
            return None

    def send_image(self, wxid_or_room: str, path: str) -> dict:
        code, data = self._req("/SendImgMsg", {"wxidorgid": wxid_or_room, "path": path}, timeout=30)
        ok = code == 200 and isinstance(data, dict) and data.get("ret") == 0
        return {"ok": ok, "code": code, "data": data}

    def send_text(self, wxid_or_room: str, text: str) -> dict:
        code, data = self._req("/SendTextMsg", {"wxidorgid": wxid_or_room, "msg": text})
        ok = code == 200 and isinstance(data, dict) and data.get("ret") == 0
        return {"ok": ok, "code": code, "data": data}


# --------------------------------------------------------------------------- #
# OneBot core / WS server / reverse client
# --------------------------------------------------------------------------- #
class ActionError(Exception):
    def __init__(self, retcode: int, message: str):
        super().__init__(message)
        self.retcode = retcode


class OneBotCore:
    def __init__(self, cfg: dict, ids: IdMap, hook: HookApi):
        self.cfg = cfg
        self.ids = ids
        self.hook = hook
        self.subscribers = set()
        self.send_lock = asyncio.Lock()
        self.recent: Dict[int, dict] = {}
        self.connected_reverse = False
        self.reverse_ws = None
        self.pending_events: List[tuple] = []
        self.started_at = time.time()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    async def publish(self, event: dict) -> None:
        self.recent[int(event.get("message_id") or 0)] = event
        if len(self.recent) > 300:
            for k in sorted(self.recent.keys())[:100]:
                self.recent.pop(k, None)
        payload = json.dumps(event, ensure_ascii=False)
        targets = list(self.subscribers)
        if self.reverse_ws is not None:
            targets.append(self.reverse_ws)
        if not targets:
            self._queue_pending(payload)
            return
        sent = False
        dead = []
        for ws in targets:
            try:
                await ws.send(payload)
                sent = True
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)
            if self.reverse_ws is ws:
                self.reverse_ws = None
        if not sent:
            self._queue_pending(payload)

    def _queue_pending(self, payload: str) -> None:
        self.pending_events.append((time.time(), payload))
        if len(self.pending_events) > 300:
            self.pending_events = self.pending_events[-300:]

    async def flush_pending(self) -> int:
        if not self.pending_events:
            return 0
        targets = list(self.subscribers)
        if self.reverse_ws is not None:
            targets.append(self.reverse_ws)
        if not targets:
            return 0
        now = time.time()
        items = [p for (t, p) in self.pending_events if now - t <= 600]
        self.pending_events.clear()
        sent = 0
        for payload in items:
            for ws in list(targets):
                try:
                    await ws.send(payload)
                    sent += 1
                    break
                except Exception:
                    self.subscribers.discard(ws)
                    if self.reverse_ws is ws:
                        self.reverse_ws = None
        if sent:
            LOG.info("flushed %d pending events after reconnect", sent)
        return sent

    async def handle_action(self, action: str, params: dict) -> dict:
        try:
            handler = getattr(self, "_a_" + str(action), None)
            if handler is None:
                return {"status": "failed", "retcode": 1404, "data": None, "message": f"unsupported: {action}"}
            data = await handler(params or {})
            return {"status": "ok", "retcode": 0, "data": data}
        except ActionError as e:
            return {"status": "failed", "retcode": e.retcode, "data": None, "message": str(e)}
        except Exception as e:
            LOG.error("action %s failed: %s", action, traceback.format_exc())
            return {"status": "failed", "retcode": 100, "data": None, "message": repr(e)}

    async def _a_get_login_info(self, p: dict) -> dict:
        return {"user_id": self.ids.self_id(), "nickname": self.hook.self_nick or self.ids.user_name(self.ids.self_id())}

    async def _a_get_status(self, p: dict) -> dict:
        ok = await asyncio.to_thread(self.hook.alive)
        return {"online": ok, "good": ok}

    async def _a_get_version_info(self, p: dict) -> dict:
        return {"app_name": "wechat-hook-bridge", "app_version": "1.0.0", "protocol_version": "v11"}

    async def _a_get_group_list(self, p: dict) -> List[dict]:
        await asyncio.to_thread(self.hook.refresh_contacts, True)
        out = []
        for wxid, c in self.hook.contacts.items():
            if not wxid.endswith("@chatroom"):
                continue
            name = c.get("nick_name") or c.get("NickName") or wxid
            out.append({"group_id": self.ids.group_id(wxid, name), "group_name": name, "member_count": 0})
        return out

    async def _a_get_friend_list(self, p: dict) -> List[dict]:
        await asyncio.to_thread(self.hook.refresh_contacts, True)
        out = []
        for wxid, c in self.hook.contacts.items():
            if wxid.endswith("@chatroom") or wxid.startswith("gh_") or wxid == "filehelper":
                continue
            name = c.get("nick_name") or c.get("NickName") or wxid
            out.append({"user_id": self.ids.user_id(wxid, name), "nickname": name, "remark": c.get("remark") or ""})
        return out

    async def _a_get_group_info(self, p: dict) -> dict:
        gid = int(p.get("group_id") or 0)
        entry = self.ids.lookup_group(gid) or {}
        return {"group_id": gid, "group_name": entry.get("name") or self.ids.group_name(gid), "member_count": 0}

    async def _a_get_group_member_info(self, p: dict) -> dict:
        uid = int(p.get("user_id") or 0)
        entry = self.ids.lookup_user(uid) or {}
        return {"group_id": int(p.get("group_id") or 0), "user_id": uid,
                "nickname": entry.get("name") or self.ids.user_name(uid), "card": "", "role": "member"}

    async def _a_get_stranger_info(self, p: dict) -> dict:
        uid = int(p.get("user_id") or 0)
        return {"user_id": uid, "nickname": self.ids.user_name(uid)}

    async def _a_get_msg(self, p: dict) -> dict:
        ev = self.recent.get(int(p.get("message_id") or 0))
        if not ev:
            raise ActionError(100, "message not found")
        return ev

    async def _a_can_send_image(self, p: dict) -> dict:
        return {"yes": True}

    async def _a_can_send_record(self, p: dict) -> dict:
        return {"yes": False}

    async def _a_send_private_msg(self, p: dict) -> dict:
        return await self._send(p, group=False)

    async def _a_send_group_msg(self, p: dict) -> dict:
        return await self._send(p, group=True)

    async def _a_send_msg(self, p: dict) -> dict:
        return await self._send(p, group=bool(p.get("group_id")))

    async def _send(self, p: dict, group: bool) -> dict:
        text, images = parse_segments(p.get("message"))
        if not text and not images:
            raise ActionError(100, "empty message")
        if group:
            gid = int(p.get("group_id") or 0)
            entry = self.ids.lookup_group(gid) or {}
            target = entry.get("chat_id") or ""
            if not target:
                raise ActionError(100, f"unknown group_id {gid}")
        else:
            uid = int(p.get("user_id") or 0)
            entry = self.ids.lookup_user(uid) or {}
            target = entry.get("wxid") or ""
            if not target:
                raise ActionError(100, f"unknown user_id {uid}")
        async with self.send_lock:
            if text:
                res = await asyncio.to_thread(self.hook.send_text, target, text)
                if not res.get("ok"):
                    raise ActionError(100, f"send failed: {res.get('data')}")
                LOG.info("out %s(%s): %s", "group" if group else "private", target, text[:80])
            for seg in images:
                path = await asyncio.to_thread(resolve_image_file, seg)
                if not path:
                    LOG.warning("skip unresolvable image segment: %s", str(seg)[:120])
                    continue
                res = await asyncio.to_thread(self.hook.send_image, target, path)
                if not res.get("ok"):
                    raise ActionError(100, f"send image failed: {res.get('data')}")
                LOG.info("out image %s(%s): %s", "group" if group else "private", target, path)
        return {"message_id": int(time.time() * 1000) % (2 ** 31)}



VOICE_SCAN_JS = r"""
rpc.exports = {
  scan: function () {
    var out = [];
    var ranges = Process.enumerateRanges('rw-');
    for (var i = 0; i < ranges.length; i++) {
      var r = ranges[i];
      if (r.size > 128 * 1024 * 1024) continue;
      try {
        var res = Memory.scanSync(r.base, r.size, "23 21 53 49 4c 4b 5f 56 33");
        if (res.length) out.push(res[0].address.toString());
      } catch (e) {}
    }
    return out;
  },
  dumphex: function (addr, size) {
    try {
      var b = ptr(addr).readByteArray(size);
      var u8 = new Uint8Array(b);
      var s = '';
      for (var i = 0; i < u8.length; i++) s += ('0' + u8[i].toString(16)).slice(-2);
      return s;
    } catch (e) { return 'ERR:' + e.message; }
  }
};
"""


def silk_exact(data: bytes):
    if not data.startswith(b"#!SILK_V3"):
        return None
    i = len(b"#!SILK_V3")
    while i + 2 <= len(data):
        n = int.from_bytes(data[i:i + 2], "little")
        i += 2
        if n <= 0 or i + n > len(data):
            break
        i += n
    return data[:i] if i > 10 else None

def parse_segments(message: Any) -> Tuple[str, List[dict]]:
    if message is None:
        return "", []
    if isinstance(message, str):
        return message, []
    parts: List[str] = []
    images: List[dict] = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            parts.append(str(d.get("text") or ""))
        elif t == "at":
            qq = d.get("qq")
            parts.append("@全体成员 " if qq == "all" else "@%s " % (d.get("name") or qq or ""))
        elif t == "image":
            images.append(d)
        elif t == "face":
            parts.append("[表情]")
        elif t == "reply":
            continue
        else:
            if d.get("text"):
                parts.append(str(d["text"]))
    return "".join(parts).strip(), images


def resolve_image_file(data: dict) -> Optional[str]:
    """Turn an OneBot image segment into a local file path the hook can send."""
    import base64
    import hashlib
    tmpdir = ROOT / "state" / "tmp_images"
    tmpdir.mkdir(parents=True, exist_ok=True)

    def save_bytes(raw: bytes, hint: str) -> str:
        ext = ".png"
        for candidate in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            if isinstance(hint, str) and hint.lower().endswith(candidate):
                ext = candidate
                break
        name = hashlib.md5(raw).hexdigest() + ext
        path = tmpdir / name
        if not path.exists():
            path.write_bytes(raw)
        return str(path)

    src = data.get("path") or data.get("file") or ""
    url = data.get("url") or ""
    if isinstance(src, str) and src.startswith("file://"):
        src = src[7:]
    if isinstance(src, str) and src.startswith("base64://"):
        try:
            return save_bytes(base64.b64decode(src[9:]), "")
        except Exception:
            return None
    if isinstance(src, str) and src and os.path.exists(src):
        return src
    for candidate in (url, src):
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(candidate, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return save_bytes(r.read(), candidate)
            except Exception as e:
                LOG.warning("image download failed: %r", e)
    return None

class WsServer:
    def __init__(self, cfg: dict, core: OneBotCore):
        self.cfg = cfg
        self.core = core
        self.server = None

    def _token_ok(self, request) -> bool:
        want = str(self.cfg["onebot"].get("ws_server", {}).get("token") or "")
        if not want:
            return True
        auth = request.headers.get("Authorization", "") or ""
        for p in ("Bearer ", "Token "):
            if auth.startswith(p) and auth[len(p):].strip() == want:
                return True
        q = parse_qs(urlparse(request.path).query)
        return (q.get("access_token") or [""])[0] == want

    async def start(self) -> None:
        host = self.cfg["onebot"]["ws_server"].get("host", "0.0.0.0")
        port = int(self.cfg["onebot"]["ws_server"].get("port", 3002))

        async def handler(ws):
            if not self._token_ok(ws.request):
                await ws.close(code=1008, reason="unauthorized")
                return
            LOG.info("ws server: client connected")
            self.core.subscribers.add(ws)
            try:
                await self.core.flush_pending()
            except Exception:
                pass
            try:
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(payload, dict) and payload.get("action"):
                        resp = await self.core.handle_action(payload["action"], payload.get("params") or {})
                        resp["echo"] = payload.get("echo")
                        await ws.send(json.dumps(resp, ensure_ascii=False))
            except Exception:
                pass
            finally:
                self.core.subscribers.discard(ws)

        self.server = await ws_serve(handler, host, port, max_size=16 * 1024 * 1024)
        LOG.info("OneBot WS server listening on ws://%s:%s", host, port)


class ReverseClient:
    def __init__(self, cfg: dict, core: OneBotCore):
        self.cfg = cfg
        self.core = core

    async def run(self) -> None:
        rc = self.cfg["onebot"].get("reverse") or {}
        url = str(rc.get("url") or "")
        if not url:
            return
        token = str(rc.get("token") or "")
        reconnect = max(2, int(rc.get("reconnect_seconds") or 5))
        while True:
            headers = {"X-Self-ID": str(self.core.ids.self_id()), "X-Client-Role": "Universal",
                       "User-Agent": "wechat-hook-bridge/1.0"}
            if token:
                headers["Authorization"] = "Bearer " + token
            try:
                async with ws_connect(url, additional_headers=headers, max_size=16 * 1024 * 1024,
                                      ping_interval=25, ping_timeout=25) as ws:
                    self.core.connected_reverse = True
                    self.core.reverse_ws = ws
                    LOG.info("reverse WS connected: %s", url)
                    try:
                        await self.core.flush_pending()
                    except Exception:
                        pass
                    await self._lifecycle(ws)
                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            await self._on_message(ws, raw)
                    finally:
                        hb.cancel()
                        if self.core.reverse_ws is ws:
                            self.core.reverse_ws = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.warning("reverse WS error: %r (retry in %ss)", e, reconnect)
            finally:
                self.core.connected_reverse = False
            await asyncio.sleep(reconnect)

    async def _on_message(self, ws, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return
        if isinstance(payload, dict) and payload.get("action"):
            resp = await self.core.handle_action(payload["action"], payload.get("params") or {})
            resp["echo"] = payload.get("echo")
            try:
                await ws.send(json.dumps(resp, ensure_ascii=False))
            except Exception:
                pass

    async def _lifecycle(self, ws) -> None:
        await ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle",
                                  "sub_type": "connect", "time": int(time.time()),
                                  "self_id": self.core.ids.self_id()}, ensure_ascii=False))

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "heartbeat",
                                          "time": int(time.time()), "self_id": self.core.ids.self_id(),
                                          "status": {"online": True, "good": True}, "interval": 30000},
                                         ensure_ascii=False))
            except Exception:
                return


# --------------------------------------------------------------------------- #
# callback HTTP server (hook -> bridge)
# --------------------------------------------------------------------------- #

def build_pat_event(cfg, ids, hook, item, self_wxid):
    pat = item.get("pat") or {}
    xml = str(item.get("msg") or "")
    if not pat and xml:
        def _tag(t):
            m = re.search(r"<%s>\s*(?:<!\[CDATA\[)?([^<\]]+)" % t, xml)
            return m.group(1).strip() if m else ""
        pat = {"from": _tag("fromusername"), "chat": _tag("chatusername"),
               "patted": _tag("pattedusername"), "template": _tag("pattemplate")}
    from_id = str(pat.get("from") or item.get("sender") or "")
    chat = str(pat.get("chat") or item.get("room") or "")
    patted = str(pat.get("patted") or "")
    is_group = chat.endswith("@chatroom")
    from_name = hook.name_of(from_id) or from_id or "某人"
    self_hit = bool(self_wxid) and patted == self_wxid
    patted_name = "你" if self_hit else (hook.name_of(patted) or patted or "某人")
    text = "[拍一拍] %s 拍了拍 %s" % (from_name, patted_name)
    if is_group:
        gname = (hook.contacts.get(chat) or {}).get("nick_name") or chat
        gid = ids.group_id(chat, gname)
        uid = ids.user_id(from_id, from_name)
        segments = []
        if self_hit:
            segments.append({"type": "at", "data": {"qq": str(ids.self_id()), "name": hook.self_nick}})
        segments.append({"type": "text", "data": {"text": text}})
        LOG.info("pat group(%s): %s", gname, text)
        return {
            "time": int(time.time()), "self_id": ids.self_id(), "post_type": "message",
            "message_type": "group", "sub_type": "normal",
            "message_id": int(time.time() * 1000) % (2 ** 31),
            "group_id": gid, "user_id": uid, "message": segments, "raw_message": text, "font": 0,
            "sender": {"user_id": uid, "nickname": from_name, "card": "", "role": "member"},
            "anonymous": None,
        }
    friend = chat or from_id
    uname = hook.name_of(friend) or friend or "某人"
    uid = ids.user_id(friend, uname)
    LOG.info("pat private(%s): %s", uname, text)
    return {
        "time": int(time.time()), "self_id": ids.self_id(), "post_type": "message",
        "message_type": "private", "sub_type": "friend",
        "message_id": int(time.time() * 1000) % (2 ** 31),
        "user_id": uid, "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text, "font": 0,
        "sender": {"user_id": uid, "nickname": uname, "card": "", "role": "member"},
    }


def build_event(cfg: dict, ids: IdMap, hook: HookApi, item: dict, self_wxid: str) -> Optional[dict]:
    msgtype = int(item.get("msgtype") or 1)
    room = str(item.get("room") or "")
    sender = str(item.get("sender") or "")
    fromid = str(item.get("fromid") or "")
    toid = str(item.get("toid") or "")
    content = str(item.get("msg") or "")

    if self_wxid and sender == self_wxid and not cfg["onebot"].get("report_self_message", False):
        return None
    if not room and fromid == self_wxid:
        return None

    if msgtype == 10000 and item.get("event") == "pat":
        return build_pat_event(cfg, ids, hook, item, self_wxid)
    is_group = bool(room)
    kind = MSG_TYPES.get(msgtype, "other")
    if kind in ("system", "recall"):
        return None
    at_self = False
    if is_group and isinstance(content, str):
        content = re.sub(r"^(?:wxid_[0-9A-Za-z_]+|[0-9A-Za-z_\-]{4,64}):\s*\n?", "", content, count=1)
        aliases = [hook.self_nick] + [str(a) for a in (cfg["hook"].get("at_self_aliases") or [])]
        for name in [a for a in aliases if a]:
            token = "@" + name
            if content.startswith(token):
                content = content[len(token):].lstrip("\u2005 \u00a0")
                at_self = True
                break

    image_path = None
    voice_path = None
    if kind == "voice":
        mlen = re.search(r'(?<![A-Za-z])length="(\d+)"', content or "")
        mms = re.search(r'voicelength="(\d+)"', content or "")
        mmd5 = re.search(r'voicemd5="([0-9a-fA-F]{16,64})"', content or "")
        if mms:
            voice_path = hook.capture_voice(int(mms.group(1)), mmd5.group(1).lower() if mmd5 else "")
        text = "[语音]" if not voice_path else "[语音]"
    elif kind == "image":
        image_path = hook.decode_image(content)
        text = "[图片]"
    elif kind == "emotion":
        image_path = hook.fetch_sticker(content)
        text = "[表情]"
    elif kind == "text":
        text = content
    elif kind in ("link", "file") and "<" in content:
        title = re.search(r"<title>(.*?)</title>", content, re.S)
        apptype = re.search(r"<type>\s*(\d+)\s*</type>", content)
        if apptype and apptype.group(1) in ("3", "6", "8"):
            text = "[图片]"
        elif title:
            text = "[链接] " + title.group(1).strip()[:60]
        else:
            text = PLACEHOLDER.get(kind) or f"[{kind}]"
    else:
        text = PLACEHOLDER.get(kind) or f"[{kind}]"

    name_cache = hook.contacts
    if is_group:
        gname = (name_cache.get(room) or {}).get("nick_name") or room
        gid = ids.group_id(room, gname)
        sender_wxid = sender or fromid
        uname = hook.name_of(sender_wxid) if sender_wxid else ""
        uid = ids.user_id(sender_wxid, uname)
        msg_id = int(item.get("msgsvrid") or 0) or int(time.time() * 1000) % (2 ** 31)
        segments = []
        if voice_path:
            segments.append({"type": "record", "data": {"file": voice_path}})
        elif image_path:
            segments.append({"type": "image", "data": {"file": image_path}})
        elif at_self:
            segments.append({"type": "at", "data": {"qq": str(ids.self_id()), "name": hook.self_nick}})
            segments.append({"type": "text", "data": {"text": " " + (text or " ")}})
        else:
            segments.append({"type": "text", "data": {"text": text}})
        event = {
            "time": int(time.time()), "self_id": ids.self_id(), "post_type": "message",
            "message_type": "group", "sub_type": "normal", "message_id": msg_id,
            "group_id": gid, "user_id": uid,
            "message": segments, "raw_message": text, "font": 0,
            "sender": {"user_id": uid, "nickname": uname or sender_wxid, "card": "", "role": "member"},
            "anonymous": None,
        }
        LOG.info("in group(%s) %s: %s", gname, uname or sender_wxid, text[:80])
    else:
        friend = fromid or sender
        uname = hook.name_of(friend)
        uid = ids.user_id(friend, uname)
        msg_id = int(item.get("msgsvrid") or 0) or int(time.time() * 1000) % (2 ** 31)
        event = {
            "time": int(time.time()), "self_id": ids.self_id(), "post_type": "message",
            "message_type": "private", "sub_type": "friend", "message_id": msg_id,
            "user_id": uid, "message": ([{"type": "record", "data": {"file": voice_path}}] if voice_path else ([{"type": "image", "data": {"file": image_path}}] if image_path else [{"type": "text", "data": {"text": text}}])),
            "raw_message": text, "font": 0,
            "sender": {"user_id": uid, "nickname": uname, "card": "", "role": "member"},
        }
        LOG.info("in private(%s) %s: %s", uname, friend, text[:80])
    return event


class CallbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, loop, core, cfg, ids, hook):
        super().__init__(addr, handler)
        self.loop = loop
        self.core = core
        self.cfg = cfg
        self.ids = ids
        self.hook = hook


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ret":0}')
        srv = self.server
        # 多开隔离：只处理本实例（机器人号）的事件。
        # 第二实例若从真实安装目录启动，会加载同一个 version.dll hook 并把
        # 自己的事件也 POST 到这个端口；用 payload.selfwxid 与配置的 self_wxid
        # 比对，把非本账号的事件直接丢弃，避免机器人用错号回复。
        ev_self = str(payload.get("selfwxid") or "")
        expect_self = str(srv.cfg.get("hook", {}).get("self_wxid") or "")
        if ev_self and expect_self and ev_self != expect_self:
            now = time.time()
            if now - getattr(srv, "_foreign_drop_log_ts", 0.0) > 60:
                srv._foreign_drop_log_ts = now
                LOG.info("dropped event from foreign instance: selfwxid=%s (expected %s)", ev_self, expect_self)
            return
        try:
            items = payload.get("msglist") or []
            if any(int(i.get("msgtype") or 0) in (3, 34, 43, 47, 49, 10000, 10002) for i in items):
                try:
                    with open(ROOT / "state" / "callback_raw.log", "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(payload, ensure_ascii=False)[:6000] + "\n")
                except Exception:
                    pass
            self_wxid = str(payload.get("selfwxid") or srv.hook.self_wxid or "")
            for item in payload.get("msglist") or []:
                ev = build_event(srv.cfg, srv.ids, srv.hook, item, self_wxid)
                if ev:
                    asyncio.run_coroutine_threadsafe(srv.core.publish(ev), srv.loop)
        except Exception:
            LOG.warning("callback handling failed: %s", traceback.format_exc())


async def status_writer(cfg: dict, core: OneBotCore) -> None:
    path = ROOT / "state" / "hook-status.json"
    while True:
        try:
            data = {
                "ts": time.time(), "self_id": core.ids.self_id(), "wxid": core.hook.self_wxid,
                "nickname": core.hook.self_nick, "hook_alive": core.hook.alive(),
                "reverse_connected": core.connected_reverse, "ws_clients": len(core.subscribers),
                "uptime_seconds": int(time.time() - core.started_at),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass
        await asyncio.sleep(10)


async def amain(cfg_path: Path) -> int:
    cfg = load_config(cfg_path)
    setup_logging(cfg)
    LOG.info("hook bridge starting; root=%s", ROOT)
    ids = IdMap(ROOT / "state" / "hook_id_map.json")
    hook = HookApi(cfg["hook"]["api_base"], cfg["hook"].get("image_root") or "", cfg["hook"].get("self_wxid") or "")
    core = OneBotCore(cfg, ids, hook)
    core.loop = asyncio.get_running_loop()

    await asyncio.to_thread(hook.refresh_contacts, True)
    prof = await asyncio.to_thread(hook.profile)
    hook.self_wxid = str(prof.get("wxid") or prof.get("username") or cfg["hook"].get("self_wxid") or "")
    hook.self_nick = str(prof.get("nick_name") or prof.get("nickname") or cfg["hook"].get("self_nickname") or "")
    cfg_self = int(cfg["onebot"].get("self_id") or 0)
    if cfg_self > 0:
        ids.set_self_id(cfg_self)
    elif hook.self_wxid and ids.self_id() <= 0:
        ids.set_self_id(stable_int(hook.self_wxid))
    LOG.info("hook profile: wxid=%s nickname=%s self_id=%s", hook.self_wxid, hook.self_nick, ids.self_id())

    srv = CallbackServer((cfg["hook"]["callback_host"], int(cfg["hook"]["callback_port"])),
                         CallbackHandler, core.loop, core, cfg, ids, hook)
    threading.Thread(target=srv.serve_forever, name="wx-callback", daemon=True).start()
    LOG.info("callback server on http://%s:%s%s", cfg["hook"]["callback_host"],
             cfg["hook"]["callback_port"], cfg["hook"]["callback_path"])

    ws = WsServer(cfg, core)
    await ws.start()
    asyncio.create_task(ReverseClient(cfg, core).run())
    asyncio.create_task(status_writer(cfg, core))

    stop = asyncio.Event()
    try:
        import signal
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except Exception:
        pass
    await stop.wait()
    srv.shutdown()
    if ws.server:
        ws.server.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "hook.json"))
    args = ap.parse_args()
    return asyncio.run(amain(Path(args.config)))


if __name__ == "__main__":
    sys.exit(main())












