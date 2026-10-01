"""元宝深度思考模式下：思考过程 与 最终答案 的容器差异。

用法（需先停掉 server.py）：
    python tools/probe_yuanbao_think.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_yuanbao import Yuanbao  # noqa: E402

BLOCKS = """() => {
  const out = [];
  for (const e of document.querySelectorAll('div, p, section, article, pre')) {
    const t = (e.innerText || '').trim();
    if (t.length < 25) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    let inner = false;
    for (const c of e.children) {
      if ((c.innerText || '').trim().length > t.length * 0.85) { inner = true; break; }
    }
    if (inner) continue;
    out.push({
      tag: e.tagName,
      cls: (e.className || '').toString().slice(0, 75),
      len: t.length,
      text: t.replace(/\\n/g, ' ').slice(0, 60)
    });
  }
  out.sort((a, b) => b.len - a.len);
  return out.slice(0, 8);
}"""


async def main() -> None:
    a = Yuanbao()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(2500)
    print("URL:", page.url)

    slow = await a.apply_pick(page)
    print("apply_pick →", slow, a.mode_report)

    try:
        await a.new_chat(page)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(1500)

    inp, via = await a.find_input(page)
    print("输入框 via =", via)
    if inp:
        await a._type(page, inp, "梨和苹果哪个更容易引起过敏？简短回答。")
        await page.wait_for_timeout(400)
        await a.send(page, "")
        print("已发送，开始按时序采样…\n")

    for i in range(9):
        await page.wait_for_timeout(7000)
        blocks = await page.evaluate(BLOCKS)
        print(f"===== t={7 * (i + 1)}s  url={page.url[-24:]}")
        for b in blocks[:3]:
            print(f"   {b['tag']:6} len={b['len']:5} {b['cls'][:60]}")
            print(f"       {b['text']!r}")

    await browser.screenshot(page, "yuanbao", "think")
    print("\n截图已存 data/shots/")
    await browser.manager.close_all()


asyncio.run(main())
