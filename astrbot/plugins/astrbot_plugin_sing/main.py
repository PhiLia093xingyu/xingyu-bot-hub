"""<BOT_NAME>的歌喉：把一句中文歌词真的唱出来。

思路：
1. 逐字用 edge-tts 生成发音
2. 用 WORLD 声码器把每个字拉长到指定的拍长，并把基频重绘到目标音高
3. 加上颤音与滑音，让音与音之间连起来
4. 拼接成一段，发成语音条
"""

import asyncio
import os
import re
import tempfile

import edge_tts
import miniaudio
import numpy as np
import pyworld as pw

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Record
from astrbot.core.platform import AstrMessageEvent

SR = 24000
FP = 5.0
VOICE = "zh-CN-XiaoyiNeural"

NOTES = {
    "C3": 130.81, "D3": 146.83, "E3": 164.81, "F3": 174.61, "G3": 196.00,
    "A3": 220.00, "B3": 246.94,
    "C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.00,
    "A4": 440.00, "B4": 493.88,
    "C5": 523.25, "D5": 587.33, "E5": 659.25, "F5": 698.46, "G5": 783.99,
    "A5": 880.00, "B5": 987.77,
}

MELODIES = {
    "twinkle": ["C4", "C4", "G4", "G4", "A4", "A4", "G4",
                "F4", "F4", "E4", "E4", "D4", "D4", "C4"],
    "farewell": ["G4", "E4", "G4", "C5", "B4", "A4", "G4",
                 "G4", "D4", "E4", "F4", "B4", "C5"],
    "ode": ["E4", "E4", "F4", "G4", "G4", "F4", "E4",
            "D4", "C4", "C4", "D4", "E4", "D4", "C4"],
    "moon": ["E4", "G4", "A4", "G4", "E4", "D4", "C4",
             "E4", "A4", "C5", "B4", "A4", "G4", "E4"],
}


def _decode(mp3_path: str) -> np.ndarray:
    d = miniaudio.decode_file(
        mp3_path,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=SR,
    )
    return np.array(d.samples, dtype=np.float64) / 32768.0


def _render_syllable(samples: np.ndarray, freq: float, dur: float,
                     prev_f: float, next_f: float) -> np.ndarray:
    idx = np.where(np.abs(samples) > 0.012)[0]
    if len(idx) == 0:
        return np.zeros(int(dur * SR))
    pad = int(0.008 * SR)
    x = samples[max(0, idx[0] - pad):min(len(samples), idx[-1] + pad)]

    f0, t = pw.harvest(x, SR, f0_floor=80.0, f0_ceil=800.0, frame_period=FP)
    sp = pw.cheaptrick(x, f0, t, SR)
    ap = pw.d4c(x, f0, t, SR)

    n = max(8, int(dur / (FP / 1000.0)))
    src = np.arange(len(sp))
    dst = np.linspace(0, len(sp) - 1, n)
    sp2 = np.stack([np.interp(dst, src, sp[:, b]) for b in range(sp.shape[1])], axis=1)
    ap2 = np.stack([np.interp(dst, src, ap[:, b]) for b in range(ap.shape[1])], axis=1)

    tt = np.arange(n) * FP / 1000.0
    vib = 1 + 0.014 * np.sin(2 * np.pi * 5.3 * tt) * np.clip((tt - 0.16) / 0.28, 0, 1)
    f0n = np.full(n, freq) * vib

    g = max(1, int(0.05 / (FP / 1000.0)))
    for k in range(min(g, n)):
        f0n[k] = prev_f + (freq - prev_f) * (k / g)
        f0n[n - 1 - k] = next_f + (freq - next_f) * (k / g)

    return pw.synthesize(f0n, sp2, ap2, SR, frame_period=FP)


async def _tts_syllable(ch: str, workdir: str, index: int):
    path = os.path.join(workdir, f"syl{index:03d}.mp3")
    for attempt in range(5):
        try:
            await edge_tts.Communicate(ch, VOICE, rate="-12%", pitch="+0Hz").save(path)
            if os.path.getsize(path) > 800:
                return path
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[sing] 第{attempt + 1}次合成失败: {e}")
        await asyncio.sleep(0.6 + attempt * 0.5)
    return None


class SingPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)

    @filter.llm_tool()
    async def sing(
        self,
        event: AstrMessageEvent,
        lyrics: str,
        melody: str = "",
        beat: float = 0.5,
    ):
        """只在用户明确要求「唱」时才调用本工具。

        判断标准：出现「唱」「唱歌」「唱一段」「唱首歌」这类要求。
        日常说话、回答、念一段、读出来，都绝对不要调用本工具——那会变成唱歌腔，很怪。
        唱歌必须用这个工具。不要用 PowerShell / System.Speech / SAPI 或任何系统自带语音合成代替，
        那些不是唱歌，声音也不对。

        Args:
            lyrics(string): 要唱的中文歌词，建议不超过 20 个字，不要带标点
            melody(string): 可选，空格分隔的音符，如 "C4 D4 E4 G4"；写成 "C4:0.3" 可以指定这个音唱多少秒，用来做节奏
            beat(number): 每个音多少秒，默认 0.5
        """
        chars = [c for c in re.sub(r"[^\u4e00-\u9fa5a-zA-Z]", "", lyrics or "")][:24]
        if not chars:
            return "没有可以唱的字。"

        seq = []
        durs = []
        if melody.strip():
            for tok in melody.split():
                tok = tok.strip().upper()
                if not tok:
                    continue
                nm, _, dv = tok.partition(":")
                if nm.strip() not in NOTES:
                    continue
                seq.append(nm.strip())
                try:
                    durs.append(float(dv))
                except Exception:  # noqa: BLE001
                    durs.append(None)
            if not seq:
                seq = list(MELODIES["twinkle"])
                durs = [None] * len(seq)
        else:
            key = "twinkle" if len(chars) <= 14 else "farewell"
            seq = list(MELODIES[key])
            durs = [None] * len(seq)
        while len(seq) < len(chars):
            seq = seq + seq
            durs = durs + durs
        seq = seq[:len(chars)]
        durs = durs[:len(chars)]

        try:
            beat = float(beat)
        except Exception:  # noqa: BLE001
            beat = 0.5
        beat = min(1.5, max(0.28, beat))

        workdir = tempfile.mkdtemp(prefix="sing_")
        parts = []
        for i, ch in enumerate(chars):
            mp3 = await _tts_syllable(ch, workdir, i)
            if not mp3:
                continue
            prev_f = NOTES[seq[i - 1]] if i > 0 else NOTES[seq[i]]
            next_f = NOTES[seq[i + 1]] if i < len(chars) - 1 else NOTES[seq[i]]
            dur = durs[i] if durs[i] and durs[i] > 0 else beat * (1.7 if i == len(chars) - 1 else 1.0)
            seg = await asyncio.get_running_loop().run_in_executor(
                None, _render_syllable, _decode(mp3), NOTES[seq[i]], dur, prev_f, next_f
            )
            seg = np.asarray(seg, dtype=np.float64)
            edge = max(8, int(0.008 * SR))
            if len(seg) > edge * 2:
                seg[:edge] *= np.linspace(0, 1, edge)
                seg[-edge:] *= np.linspace(1, 0, edge)
            if i == len(chars) - 1:
                fade = int(0.3 * SR)
                if len(seg) > fade:
                    seg[-fade:] *= np.linspace(1, 0, fade)
            parts.append(seg)
            parts.append(np.zeros(int(0.04 * SR)))

        if not parts:
            return "嗓子今天不太行，唱不出来。"

        audio = np.concatenate(parts)
        peak = np.max(np.abs(audio)) or 1.0
        audio = np.clip(audio / peak * 0.72, -1, 1)

        import wave

        wav_path = os.path.join(workdir, "song.wav")
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((audio * 32767).astype("<i2").tobytes())

        await event.send(event.chain_result([Record(file=wav_path)]))
        return f"唱完了：{''.join(chars)}"

    async def terminate(self):
        pass
