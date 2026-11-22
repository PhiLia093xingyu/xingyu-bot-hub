"""手动跑一遍 worker：加载模型 → 画一张 → 报告耗时。"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = r"C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378\data\models\sd15"
TORCH = r"D:\astrbot-cuda"
OUT = os.path.join(os.environ["TEMP"], "sd_selftest.png")

env = os.environ.copy()
env["SD_MODEL_PATH"] = MODEL
env["SD_TORCH_PATH"] = TORCH
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"

t0 = time.time()
proc = subprocess.Popen(
    [sys.executable, "-u", os.path.join(HERE, "sd_worker.py")],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=env,
    cwd=HERE,
)

line = proc.stdout.readline().decode("utf-8")
print(f"[{time.time()-t0:.1f}s] ready line: {line.strip()[:300]}")
info = json.loads(line)
if not info.get("ok"):
    err = proc.stderr.read().decode("utf-8", "ignore")
    print("stderr:", err[-2000:])
    proc.kill()
    sys.exit(1)

req = {
    "prompt": os.environ.get(
        "SD_PROMPT",
        "1girl, cat ears, orange hair, purple pajamas, lying on bed, soft lighting, masterpiece, best quality",
    ),
    "negative": "lowres, bad anatomy, bad hands, worst quality, watermark",
    "steps": int(os.environ.get("SD_STEPS", "20")),
    "width": 512,
    "height": 512,
    "out": OUT,
}
print(f"[{time.time()-t0:.1f}s] sending request...")
proc.stdin.write((json.dumps(req) + "\n").encode())
proc.stdin.flush()
resp = json.loads(proc.stdout.readline().decode("utf-8"))
print(f"[{time.time()-t0:.1f}s] response: {json.dumps(resp, ensure_ascii=False)[:300]}")

proc.stdin.write(b'{"cmd":"quit"}\n')
proc.stdin.flush()
proc.wait(timeout=20)
if resp.get("ok"):
    print("图片:", OUT, os.path.getsize(OUT), "bytes")
