"""SD1.5 生成 worker：常驻进程，stdin 读 JSON 请求，stdout 回 JSON。

环境变量：
    SD_MODEL_PATH   diffusers 模型目录
    SD_TORCH_PATH   额外的 site-packages（CUDA 版 torch 装在这里）
    SD_DEVICE       cuda / cpu（默认自动）
"""

import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.environ.get("SD_TORCH_PATH", ""))

MODEL = os.environ.get("SD_MODEL_PATH", "")
DEVICE = os.environ.get("SD_DEVICE", "auto")


def reply(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    try:
        import torch
    except Exception as exc:  # noqa: BLE001
        reply({"ok": False, "stage": "import", "error": f"{type(exc).__name__}: {exc}"})
        return

    if DEVICE == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = DEVICE
    dtype = torch.float16 if device == "cuda" else torch.float32

    try:
        from diffusers import StableDiffusionPipeline
    except Exception as exc:  # noqa: BLE001
        reply({"ok": False, "stage": "import_diffusers", "error": f"{type(exc).__name__}: {exc}"})
        return

    t0 = time.time()
    try:
        pipe = StableDiffusionPipeline.from_pretrained(
            MODEL,
            torch_dtype=dtype,
            safety_checker=None,
            requires_safety_checker=False,
            local_files_only=True,
        )
        pipe = pipe.to(device)
        pipe.set_progress_bar_config(disable=True)
        if device == "cuda":
            pipe.enable_attention_slicing()
    except Exception as exc:  # noqa: BLE001
        reply({
            "ok": False,
            "stage": "load",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-1500:],
        })
        return

    gpu = torch.cuda.get_device_name(0) if device == "cuda" else "CPU"
    reply({
        "ok": True,
        "ready": True,
        "device": device,
        "gpu": gpu,
        "load_seconds": round(time.time() - t0, 1),
        "torch": torch.__version__,
    })

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:  # noqa: BLE001
            reply({"ok": False, "error": f"bad json: {exc}"})
            continue
        if req.get("cmd") == "quit":
            reply({"ok": True, "bye": True})
            return
        if req.get("cmd") == "ping":
            reply({"ok": True, "pong": True, "device": device})
            continue

        prompt = (req.get("prompt") or "").strip()
        if not prompt:
            reply({"ok": False, "error": "prompt 为空"})
            continue

        steps = int(req.get("steps") or 24)
        width = int(req.get("width") or 512)
        height = int(req.get("height") or 512)
        seed = int(req.get("seed") if req.get("seed") is not None else -1)
        out = req.get("out") or ""
        negative = req.get("negative") or ""

        try:
            generator = None
            if seed >= 0:
                generator = torch.Generator(device=device).manual_seed(seed)
            t1 = time.time()
            with torch.inference_mode():
                result = pipe(
                    prompt=prompt,
                    negative_prompt=negative or None,
                    num_inference_steps=steps,
                    width=width,
                    height=height,
                    guidance_scale=float(req.get("guidance") or 7.5),
                    generator=generator,
                )
            image = result.images[0]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            image.save(out)
            reply({
                "ok": True,
                "path": out,
                "seconds": round(time.time() - t1, 1),
                "seed": seed,
                "size": f"{width}x{height}",
                "steps": steps,
            })
        except Exception as exc:  # noqa: BLE001
            if "out of memory" in str(exc).lower() and device == "cuda":
                torch.cuda.empty_cache()
            reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
