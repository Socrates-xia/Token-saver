"""观察豆包发问后 DOM 的变化时序，找出"助手消息"的真实容器。

用法（需先停掉 server.py）：
    python tools/probe_doubao_flow.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_doubao import Doubao  # noqa: E402

# 抓页面上文字量较大的块，看谁是答案、谁是提问
SNAP = """() => {
  const out = [];
  for (const e of document.querySelectorAll('div, article, section')) {
    const raw = (e.innerText || '').trim();
    if (raw.length < 15 || raw.length > 3000) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    // 只要"最内层"的块：子元素里没有同样文字的
    let inner = false;
    for (const c of e.children) {
      if ((c.innerText || '').trim().length > raw.length * 0.8) { inner = true; break; }
    }
    if (inner) continue;
    out.push({
      tag: e.tagName,
      cls: (e.className || '').toString().slice(0, 70),
      len: raw.length,
      text: raw.replace(/\\n/g, ' ').slice(0, 60),
      testid: e.getAttribute('data-testid') || '',
      role: e.getAttribute('data-message-author-role')
            || e.getAttribute('data-role') || ''
    });
  }
  out.sort((a, b) => b.len - a.len);
  return out.slice(0, 8);
}"""


async def main() -> None:
    a = Doubao()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(2500)

    try:
        await a.new_chat(page)
    except Exception as e:  # noqa: BLE001
        print("new_chat 失败:", e)
    await page.wait_for_timeout(1500)

    inp, via = await a.find_input(page)
    print("输入框 via =", via)
    if inp is None:
        print("找不到输入框"); return
    await a._type(page, inp, "今天郑州天气怎么样？一句话回答。")
    await page.wait_for_timeout(300)
    await a.send(page, "")
    print("已发送，开始按时序采样...\n")

    for i in range(14):
        await page.wait_for_timeout(2000)
        blocks = await page.evaluate(SNAP)
        print(f"--- t={2*(i+1)}s  url={page.url.replace('https://www.doubao.com','')}")
        for b in blocks[:4]:
            print(f"    {b['tag']:6} len={b['len']:5} testid={b['testid'][:24]:24} "
                  f"role={b['role'][:10]:10} cls={b['cls'][:40]}")
            print(f"        {b['text']!r}")

    await browser.screenshot(page, "doubao", "flow")
    print("\n截图已存 data/shots/")
    await browser.manager.close_all()


asyncio.run(main())
