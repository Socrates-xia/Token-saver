"""排查豆包的模型/思考模式下拉。

用法（需先停掉 server.py，profile 独占）：
    python tools/probe_doubao.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_doubao import Doubao  # noqa: E402

DUMP = """() => {
  const out = [];
  for (const e of document.querySelectorAll('*')) {
    const t = (e.textContent || '').trim();
    if (!t || t.length > 20) continue;
    if (!t.includes('快速') && !t.includes('思考')) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    out.push({tag: e.tagName, text: t,
              cls: (e.className || '').toString().slice(0, 55),
              ds: e.getAttribute('data-state') || '',
              area: Math.round(r.width * r.height),
              html: e.outerHTML.replace(/\\s+/g, ' ').slice(0, 200)});
  }
  out.sort((a, b) => a.area - b.area);
  return out.slice(0, 10);
}"""


async def main() -> None:
    a = Doubao()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(3000)

    items = await page.evaluate(DUMP)
    print(f"含『快速/思考』的元素 {len(items)} 个：")
    for it in items:
        print(f"\n  {it['tag']:5} | {it['text']:6} | data-state={it['ds']!r} | "
              f"area={it['area']} | {it['cls'][:40]}")
        print(f"    {it['html'][:180]}")

    # 找一个带 data-state 的当触发器
    cands = [i for i in items if i["ds"]]
    print(f"\n带 data-state 的候选：{len(cands)} 个")
    if not cands:
        await browser.manager.close_all()
        return

    trig = page.locator("[data-state]:has-text('快速')").first
    if await trig.count() == 0:
        trig = page.locator("[data-state]").first
    print("触发器 data-state（点前）:", await trig.get_attribute("data-state"))
    await trig.click()
    await page.wait_for_timeout(1500)
    print("触发器 data-state（点后）:", await trig.get_attribute("data-state"))

    menu = await page.evaluate(DUMP)
    print(f"\n点击后含『快速/思考』的元素 {len(menu)} 个：")
    for it in menu:
        print(f"  {it['tag']:5} | {it['text']:8} | ds={it['ds']!r} | area={it['area']}")
    print("\n文字匹配『深度思考』的元素：",
          await page.locator("text=深度思考").count())

    await browser.screenshot(page, "doubao", "probe")
    print("截图已存 data/shots/")
    await browser.manager.close_all()


asyncio.run(main())
