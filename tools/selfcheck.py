"""自检脚本：不用开控制台也能验证环境是否就绪。

用法：
    python tools/selfcheck.py            # 检查 Edge/Playwright + 页面可达性
    python tools/selfcheck.py headful    # 用带窗口模式试（会真的弹出浏览器）

退出码：0 = 至少一种模式能驱动浏览器；1 = 两种都失败。

能验什么 / 不能验什么（重要）
-----------------------------
这个脚本回答的是「**浏览器驱动得起来吗**」—— 装完环境后先跑它，排查的是
Playwright / Edge 通道 / 内核缺失这类问题。

它**不回答**「站点登录了吗」。为了不碰你真实的浏览器 profile，它刻意用一个
一次性的空 profile（`data/profiles/_selfcheck`），所以那必然是个**未登录**的
页面：输出里 `textarea=0 contenteditable=0` 是**正常的**，不是故障信号。
判断登录状态请看控制台的「站点管理」，或 `tools/verify_session.py`。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser, settings  # noqa: E402


async def try_launch(headless: bool) -> tuple[bool, str]:
    pw = await browser.async_playwright().start()
    tmpdir = settings.PROFILE_DIR / "_selfcheck"
    try:
        kwargs = dict(
            user_data_dir=str(tmpdir),
            headless=headless,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        )
        channel = settings.get()["browser"]["channel"]
        if channel:
            kwargs["channel"] = channel
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://chat.deepseek.com",
                        wait_until="domcontentloaded", timeout=45000)
        title = await page.title()
        n_input = await page.locator("textarea").count()
        n_edit = await page.locator("div[contenteditable='true']").count()
        await ctx.close()
        return True, f"页面标题「{title}」 textarea={n_input} contenteditable={n_edit}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:220]}"
    finally:
        await pw.stop()
        # 这个一次性 profile 是几百 MB 级别的垃圾，用完就删。
        # 删不掉也不该让自检失败（Edge 可能还攥着文件句柄），所以吞掉异常；
        # 但下次运行会复用同一个目录，不会越堆越多。
        shutil.rmtree(tmpdir, ignore_errors=True)


async def main() -> int:
    headful_requested = "headful" in sys.argv
    print("[1/2] 无头模式尝试 …")
    ok1, msg1 = await try_launch(True)
    print("      结果:", "OK" if ok1 else "FAIL", "-", msg1)

    print("[2/2] 带窗口模式尝试 …")
    ok2, msg2 = await try_launch(False)
    print("      结果:", "OK" if ok2 else "FAIL", "-", msg2)

    print("\n结论：")
    if ok1 or ok2:
        print("  浏览器可驱动。若两种都 OK，config.yaml 里 headless 可保持 false（可见，更稳）。")
        print("  注：上面 textarea / contenteditable 为 0 属正常 —— 自检用的是"
              "一次性空 profile（未登录），")
        print("      那几个计数不表示站点可用。登录状态请在控制台「站点管理」里看。")
    else:
        print("  都失败了。多半是本机没有 Playwright 内核且 Edge 通道不可用，")
        print("  执行： python -m playwright install chromium")

    if headful_requested and not ok2:
        print("  （已按你的要求试了带窗口模式）")

    # 两种模式都失败 => 退出码 1，方便脚本/CI 判定。
    # 这里以前是返回 None，退出码恒为 0，"FAIL" 只能靠人眼在输出里找。
    return 0 if (ok1 or ok2) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

