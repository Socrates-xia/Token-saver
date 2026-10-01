"""把 Kimi 的模型/模式菜单点开，列出所有选项。

用法（需先停掉 server.py）：
    python tools/probe_kimi_menu.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_kimi import Kimi  # noqa: E402

TEXTS = """() => {
  const out = [];
  for (const e of document.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const t = (e.textContent || '').trim();
    if (!t || t.length > 40) continue;
    if (e.children.length > 0 && t.length > 16) continue;
    out.push({tag: e.tagName,
              cls: (e.className || '').toString().slice(0, 45),
              role: e.getAttribute('role') || '',
              state: e.getAttribute('aria-checked') || e.getAttribute('aria-selected') || '',
              text: t});
  }
  return out.slice(0, 150);
}"""


async def main() -> None:
    a = Kimi()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(3000)
    print("URL:", page.url)

    trig = page.locator("[data-testid='model-select-trigger']")
    n = await trig.count()
    print(f"model-select-trigger 数量: {n}")
    if n == 0:
        print("页面上没有这个触发器，先把页面滚到输入框附近或手动打开一次对话")
        await browser.manager.close_all()
        return
    print("点击前 aria-expanded =", await trig.first.get_attribute("aria-expanded"))

    await trig.first.click()
    await page.wait_for_timeout(1800)
    print("点击后 aria-expanded =", await trig.first.get_attribute("aria-expanded"))

    # 菜单本体
    for sel in ("#v-3-menu", "[role='menu']", ".kimi-menu", ".next-menu"):
        c = await page.locator(sel).count()
        print(f"  {sel:16} 命中 {c} 个")

    items = await page.evaluate(TEXTS)
    print(f"\n可见短文本元素 {len(items)} 个：")
    for it in items:
        print(f"  {it['tag']:6} | {it['role']:8} | st={it['state']:6} | {it['text'][:26]:28} | {it['cls'][:32]}")

    await browser.screenshot(page, "kimi", "menu")
    print("\n截图已存 data/shots/")
    await browser.manager.close_all()


asyncio.run(main())
