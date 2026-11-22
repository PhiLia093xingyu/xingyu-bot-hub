"""用 Playwright 自带的 Chromium 截支付宝付款码。

为什么不用系统 Edge：
  AstrBot 现在以 SYSTEM 身份、在 session 0 里跑，Edge 拒绝启动（exit 1002），
  而且 SYSTEM 解不开 <USER> 那份 DPAPI 加密的登录态。
  所以这里自带一套 Chromium，登录态存在插件自己的 profile 里，
  第一次用微信扫码登录一次即可，之后都能无头复用。
"""

from __future__ import annotations

import os
from astrbot.api import logger

TOP_UP_URL = "https://platform.deepseek.com/top_up"

# Chromium 放在 D 盘，免得哪天 C 盘清理时又被当成"可再生成缓存"删掉
_BROWSER_HOMES = (
    r"D:\C_Rebalance\ms-playwright",
    os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright"),
    r"C:\Windows\system32\config\systemprofile\AppData\Local\ms-playwright",
)
for _home in _BROWSER_HOMES:
    if os.path.isdir(_home):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _home
        break

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0 Safari/537.36 Edg/134.0"
)


def _launch(profile_dir: str, headless: bool = True):
    from playwright.sync_api import sync_playwright

    os.makedirs(profile_dir, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        profile_dir,
        headless=headless,
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        viewport={"width": 1100, "height": 1400},
        device_scale_factor=2,
        user_agent=UA,
        locale="zh-CN",
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


def _close(pw, ctx) -> None:
    try:
        ctx.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass


def is_login_page(page) -> bool:
    try:
        text = page.evaluate("document.body.innerText.slice(0, 400)")
    except Exception:
        return False
    return "请输入手机号" in text or "微信扫码登录" in text or "发送验证码" in text


def grab_login_qr(profile_dir: str, out_path: str, timeout: float = 30.0) -> str:
    """截一张微信登录二维码，给用户扫。"""
    pw, ctx, page = _launch(profile_dir)
    try:
        page.goto(TOP_UP_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(3000)
        if not is_login_page(page):
            raise RuntimeError("已经是登录状态，不用再扫码")
        # 二维码是页面右侧那块；用 canvas/img 兜底
        box = None
        try:
            el = page.locator("img[src^='data:image'], canvas").last
            box = el.bounding_box()
            if box and box["width"] > 80:
                el.screenshot(path=out_path)
                return out_path
        except Exception:
            box = None
        page.screenshot(path=out_path)
        return out_path
    finally:
        _close(pw, ctx)


def capture_alipay_qr(
    profile_dir: str,
    out_path: str,
    amount: int = 10,
    *,
    timeout: float = 60.0,
) -> str:
    """打开充值页 -> 点金额 -> 点去支付 -> 截下付款码画布。失败抛异常。"""
    pw, ctx, page = _launch(profile_dir)
    try:
        page.goto(TOP_UP_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(2500)
        if is_login_page(page):
            raise RuntimeError("登录态失效，需要重新用微信扫码登录")

        page.wait_for_function(
            "document.body.innerText.includes('去支付')", timeout=timeout * 1000
        )
        if amount:
            try:
                page.get_by_text(f"¥{amount}", exact=True).first.click(timeout=5000)
                page.wait_for_timeout(600)
            except Exception:
                logger.warning(f"[xingyu_dinner] 点金额 ¥{amount} 失败，用页面默认档位")
        page.get_by_text("去支付", exact=True).last.click(timeout=10000)

        page.wait_for_selector("canvas", timeout=15000)
        page.wait_for_timeout(1200)
        canvas = page.locator("canvas").last
        box = canvas.bounding_box()
        if not box or box["width"] < 60:
            raise RuntimeError("付款码画布尺寸异常")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        canvas.screenshot(path=out_path)
        logger.info(f"[xingyu_dinner] 付款码已截取: {out_path}")
        return out_path
    finally:
        _close(pw, ctx)
