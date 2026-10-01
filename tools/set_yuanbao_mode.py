"""把元宝的思考模式切到指定项（用于构造测试前置状态）。

用法（需先停掉 server.py）：
    python tools/set_yuanbao_mode.py 快速回答
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_yuanbao import Yuanbao  # noqa: E402


async def main() -> None:
    want = sys.argv[1] if len(sys.argv) > 1 else "快速回答"
    a = Yuanbao()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(2500)

    btn = page.locator("[data-thinking-mode-switcher-trigger='true']").first
    if await btn.count() == 0:
        print("没找到切换按钮")
        return
    print("当前模式:", repr((await btn.text_content() or "").strip()))
    if want in ((await btn.text_content()) or ""):
        print("已经是目标模式，不用切")
        await browser.manager.close_all()
        return

    await btn.click()
    await page.wait_for_timeout(1500)
    item = page.locator(f"text={want}").last
    if await item.count() == 0:
        print(f"菜单里没有『{want}』")
        await browser.manager.close_all()
        return
    await item.click()
    await page.wait_for_timeout(1200)
    print("切换后模式:", repr((await btn.text_content() or "").strip()))
    await browser.manager.close_all()


asyncio.run(main())
