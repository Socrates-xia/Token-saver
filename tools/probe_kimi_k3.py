"""K3 模式下提问一次，找出答案容器的真实结构。

用法（需先停掉 server.py）：
    python tools/probe_kimi_k3.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_kimi import Kimi  # noqa: E402

BLOCKS = """() => {
  const out = [];
  for (const e of document.querySelectorAll('div, p, section, article')) {
    const t = (e.innerText || '').trim();
    if (t.length < 40) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    let inner = false;
    for (const c of e.children) {
      if ((c.innerText || '').trim().length > t.length * 0.85) { inner = true; break; }
    }
    if (inner) continue;
    out.push({tag: e.tagName,
              cls: (e.className || '').toString().slice(0, 70),
              role: e.getAttribute('data-role') || e.getAttribute('data-testid') || '',
              len: t.length,
              text: t.replace(/\\n/g, ' ').slice(0, 70)});
  }
  out.sort((a, b) => b.len - a.len);
  return out.slice(0, 10);
}"""


async def main() -> None:
    a = Kimi()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(3000)
    print("URL:", page.url)

    slow = await a.apply_pick(page)
    print("apply_pick →", slow, a.mode_report)
    trig = page.locator("[data-testid='model-select-trigger']").first
    if await trig.count():
        print("当前模型按钮文字:", repr((await trig.text_content() or "").strip()))

    try:
        await a.new_chat(page)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(1500)

    inp, via = await a.find_input(page)
    print("输入框 via =", via)
    if inp:
        await a._type(page, inp, "生理期适合吃什么？简短说三点。")
        await page.wait_for_timeout(400)
        await a.send(page, "")
        print("已发送，等 35s ...")
        await page.wait_for_timeout(35000)

    print("\n当前 URL:", page.url)
    blocks = await page.evaluate(BLOCKS)
    print(f"长文本块 {len(blocks)} 个：")
    for b in blocks[:6]:
        print(f"  {b['tag']:6} len={b['len']:5} role={b['role'][:18]:18} {b['cls'][:52]}")
        print(f"      {b['text']!r}")

    ans, evia = await a.extract(page)
    print(f"\nextract → via={evia} len={len(ans)}")
    print("  前 120 字:", repr(ans[:120]))

    await browser.screenshot(page, "kimi", "k3")
    print("\n截图已存 data/shots/")
    await browser.manager.close_all()


asyncio.run(main())
