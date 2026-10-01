"""探测豆包的生图流程：发提示词 → 观察 img 元素何时出现、src 长什么样。

用法（需先停掉 server.py）：
    python tools/probe_doubao_image.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_doubao import Doubao  # noqa: E402

IMGS = """() => Array.from(document.querySelectorAll('img'))
  .filter(e => e.offsetParent)
  .map(e => {
    const r = e.getBoundingClientRect();
    return {
      src: (e.currentSrc || e.src || '').slice(0, 160),
      nat: e.naturalWidth + 'x' + e.naturalHeight,
      disp: Math.round(r.width) + 'x' + Math.round(r.height),
      alt: (e.alt || '').slice(0, 30)
    };
  })
  .filter(i => parseInt(i.disp) >= 60)"""


async def main() -> None:
    a = Doubao()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(2500)
    try:
        await a.new_chat(page)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(1200)

    inp, _ = await a.find_input(page)
    if not inp:
        print("找不到输入框"); return
    await a._type(page, inp, "帮我生成一张奶龙的图片")
    await page.wait_for_timeout(400)
    await a.send(page, "")
    print("已发送，开始观察 img ...\n")

    for i in range(14):
        await page.wait_for_timeout(7000)
        imgs = await page.evaluate(IMGS)
        print(f"--- t={7*(i+1)}s  img 数={len(imgs)}")
        for im in imgs[:6]:
            print(f"    nat={im['nat']:10} disp={im['disp']:10} alt={im['alt']!r}")
            print(f"        {im['src']}")

    await browser.screenshot(page, "doubao", "img")
    print("\n截图已存 data/shots/")
    print("URL:", page.url)
    await browser.manager.close_all()


asyncio.run(main())
