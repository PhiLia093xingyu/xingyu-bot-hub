# 本地画图（astrbot_plugin_local_sd）

把 `data/models/sd15`（SD1.5 diffusers）跑成常驻 worker，给 AstrBot 的 agent 一个
`local_draw` 工具，另带 `/画画` 手动命令。图直接发进当前会话。

## 为什么用子进程

- 模型加载 + 推理全在 **独立 worker 进程**里，AstrBot 主进程不背 4~6 GB 显存/内存，
  也不会被 20~30 秒的采样卡住事件循环
- worker 按需启动（第一次画图时冷启动，之后常驻），请求串行排队，避免把 6 GB 显存打爆
- worker 崩了不影响 bot：下次调用会自动重拉

## 依赖

| 项 | 说明 |
| --- | --- |
| 模型 | `<AstrBot>/data/models/sd15`（diffusers 格式，约 4 GB，机器上已有） |
| torch CUDA | `D:\astrbot-cuda`：torch 2.5.1+cu124（4.33 GB，实际靠 GTX 1060 跑） |
| diffusers 栈 | 同一目录里另装了 diffusers 0.31.0 + transformers 4.46.3 + tokenizers 0.20.3 + accelerate 1.1.1 |

为什么要单独装一套 diffusers：venv 里的 diffusers 0.42 用到了 `torch.accelerator`，
那是 torch ≥2.6 才有的 API，配 2.5.1 直接 import 就炸（`AttributeError: module 'torch' has no attribute 'accelerator'`）。
worker 进程把 `D:\astrbot-cuda` 插到 `sys.path` 最前面，所以只有跑图的子进程用这套老组合，
AstrBot 主进程仍然用它自己的新版 diffusers，互不影响。

## 实测（P106-100 / 6 GB）

| 项 | 数字 |
| --- | --- |
| 冷启动加载模型 | 11.1 s（之后有系统文件缓存，约 2 s） |
| 512×512 / 20 步 | 18.6 s |
| 512×512 / 24 步 | 21.8 s |
| 显存占用 | 约 2.5 GB（fp16） |

SD1.5 原版偏写实，所以默认加了 `style_prefix = "anime illustration, "` 才是二次元画风。

## 配置（面板「插件配置」）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `model_path` | 空 | 空 = `<AstrBot>/data/models/sd15` |
| `torch_path` | `D:\astrbot-cuda` | CUDA torch 的 `--target` 目录 |
| `device` | auto | auto / cuda / cpu |
| `steps` / `size` | 24 / 512 | SD1.5 原生 512，768 容易画崩 |
| `guidance` | 7.5 | CFG |
| `negative` | 内置一套 | 低质量、水印、多余手指等 |
| `timeout` / `load_timeout` | 240 / 300 | 推理超时 / 冷启动超时 |
| `max_size` | 768 | 硬上限 |
| `cooldown` | 10 | 同会话冷却秒数 |

## 用法

- agent 自己判断：用户说"画一张""给我生成一张猫耳少女"，它会调 `local_draw`
- 手动：`/画画 1girl, cat ears, orange hair, pajamas, soft lighting, masterpiece`
- 状态：`/画图状态`

提示词英文效果最好（SD1.5 的 CLIP 只认英文），中文会画飞。

## worker 协议

stdin/stdout，一行一个 JSON：

```json
{"prompt": "...", "negative": "...", "steps": 24, "width": 512, "height": 512, "out": "D:\\...png"}
{"ok": true, "path": "...", "seconds": 6.2, "size": "512x512", "steps": 24}
```

启动完成会先吐一行 `{"ok": true, "ready": true, "device": "cuda", "gpu": "NVIDIA P106-100", ...}`。
`{"cmd": "quit"}` 退出。

## 手动跑一次 worker

```powershell
$env:SD_MODEL_PATH = "C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378\data\models\sd15"
$env:SD_TORCH_PATH = "D:\astrbot-cuda"
$py = "C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378\.venv\Scripts\python.exe"
'{"prompt":"1girl, cat ears, orange hair, masterpiece"}' | & $py -u .\sd_worker.py
```
