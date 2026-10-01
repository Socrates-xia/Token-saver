"""排查元宝的"切换模型"下拉：点开前后分别看 aria-expanded 和菜单项。

用法（需先停掉 server.py，profile 是独占的）：
    python tools/probe_yuanbao.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_yuanbao import Yuanbao  # noqa: E402

DUMP_JS = """() => {
  const out = [];
  for (const e of document.querySelectorAll('*')) {
    const t = (e.textContent || '').trim();
    if (!t || t.length > 40) continue;
    if (!t.includes('深度思考') && !t.includes('快速回答') && !t.includes('专家模式')) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const st = {};
    for (const a of ['aria-checked','aria-selected','data-state','data-active']) {
      const v = e.getAttribute(a);
      if (v) st[a] = v;
    }
    out.push({tag: e.tagName, text: t,
              cls: (e.className || '').toString().slice(0, 60),
              state: st, area: Math.round(r.width * r.height)});
  }
  out.sort((a, b) => a.area - b.area);
  return out.slice(0, 15);
}"""


async def main() -> None:
    a = Yuanbao()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(3000)

    btn = page.locator("[data-thinking-mode-switcher-trigger='true']").first
    n = await btn.count()
    print("触发按钮数量:", n)
    if n == 0:
        print("页面 URL:", page.url)
        return
    print("点击前 aria-expanded =", await btn.get_attribute("aria-expanded"))
    print("点击前按钮文字 =", repr((await btn.text_content() or "").strip()))

    await btn.click()
    await page.wait_for_timeout(1500)
    print("点击后 aria-expanded =", await btn.get_attribute("aria-expanded"))

    items = await page.evaluate(DUMP_JS)
    print(f"\n菜单/触发相关元素 {len(items)} 个：")
    for it in items:
        print(f"  {it['tag']:5} | {it['text'][:26]:28} | st={it['state']} | "
              f"area={it['area']:6} | {it['cls'][:40]}")

    # 找到"深度思考"项并点它
    target = page.locator("text=深度思考").last
    if await target.count():
        print("\n尝试点击『深度思考』...")
        await target.click()
        await page.wait_for_timeout(1200)
        print("点击后 aria-expanded =", await btn.get_attribute("aria-expanded"))
        print("点击后按钮文字 =", repr((await btn.text_content() or "").strip()))
    else:
        print("\n没找到『深度思考』菜单项")

    await browser.screenshot(page, "yuanbao", "probe")
    print("\n截图已存 data/shots/")
    print("页面 URL:", page.url)
    await browser.manager.close_all()


asyncio.run(main())
