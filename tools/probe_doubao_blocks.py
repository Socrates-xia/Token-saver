"""列出豆包助手消息里的块类型（data-plugin-identifier），定位"正文块"。

用法（需先停掉 server.py）：
    python tools/probe_doubao_blocks.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_doubao import Doubao  # noqa: E402

JS = """() => {
  const msgs = document.querySelectorAll("[data-testid='receive_message']");
  if (!msgs.length) return {msgs: 0, blocks: []};
  const last = msgs[msgs.length - 1];
  const blocks = [];
  for (const e of last.querySelectorAll('*')) {
    const pid = e.getAttribute('data-plugin-identifier');
    if (!pid) continue;
    const t = (e.innerText || '').trim();
    const r = e.getBoundingClientRect();
    blocks.push({
      pid: pid.slice(0, 60),
      tag: e.tagName,
      len: t.length,
      html_len: e.outerHTML.length,
      text: t.replace(/\\n/g, ' | ').slice(0, 70),
      dt: e.getAttribute('data-container-type') || ''
    });
  }
  return {
    msgs: msgs.length,
    msg_text: (last.innerText || '').trim().replace(/\\n/g, ' | ').slice(0, 160),
    blocks: blocks
  };
}"""


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
    if inp:
        await a._type(page, inp, "今天郑州天气怎么样？")
        await page.wait_for_timeout(300)
        await a.send(page, "")

    for t in (15, 25, 40, 55):
        await page.wait_for_timeout(t * 1000 if t == 15 else 10000)
        r = await page.evaluate(JS)
        print(f"\n===== t≈{t}s  助手消息数={r['msgs']}")
        print("  最后一条消息文本:", repr(r.get("msg_text", ""))[:150])
        for b in r.get("blocks", []):
            print(f"   pid={b['pid']}")
            print(f"       len={b['len']:5} html={b['html_len']:6} "
                  f"dt={b['dt'][:18]:18} {b['text']!r}")

    await browser.screenshot(page, "doubao", "blocks")
    await browser.manager.close_all()


asyncio.run(main())
