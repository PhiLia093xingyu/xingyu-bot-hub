"""临时工具：开一个持久化的无头 Chromium，把微信登录二维码截出来。"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from payqr import grab_login_qr, is_login_page, _launch, _close, TOP_UP_URL

PROFILE = r"C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378\data\plugin_data\astrbot_plugin_xingyu_dinner\pwprofile"
OUT = r"C:\ProgramData\NapCatQQ Desktop\apps\astrbot\6a0fc378\data\plugin_data\astrbot_plugin_xingyu_dinner\login_qr.png"
WAIT = int(sys.argv[1]) if len(sys.argv) > 1 else 240

pw, ctx, page = _launch(PROFILE)
try:
    page.goto(TOP_UP_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    if not is_login_page(page):
        print("ALREADY_LOGGED_IN")
        sys.exit(0)
    page.screenshot(path=OUT, full_page=False)
    print("QR_SAVED", OUT, flush=True)
    deadline = time.time() + WAIT
    while time.time() < deadline:
        page.wait_for_timeout(3000)
        if not is_login_page(page):
            print("LOGIN_OK", flush=True)
            # 落一下首页，让登录态写盘
            page.wait_for_timeout(2000)
            break
    else:
        print("TIMEOUT", flush=True)
finally:
    _close(pw, ctx)
