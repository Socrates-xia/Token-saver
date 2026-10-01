"""细查豆包答案块的 DOM 结构，找可辨识的特征。

用法（需先停掉 server.py）：
    python tools/probe_doubao_dom.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_doubao import Doubao  # noqa: E402

JS = """() => {
  // 找到含"气温"或"℃"的最内层块 —— 那就是答案正文
  let hit = null;
  for (const e of document.querySelectorAll('div, p, article, section')) {
    const t = (e.innerText || '').trim();
    if (t.length < 10 || t.length > 2000) continue;
    if (!/[℃度]|气温|多云|小雨|晴/.test(t)) continue;
    if (/新对话|定时任务|云盘/.test(t)) continue;
    let inner = false;
    for (const c of e.children) {
      if ((c.innerText || '').trim().length > t.length * 0.8) { inner = true; break; }
    }
    if (inner) continue;
    hit = e; break;
  }
  if (!hit) return {found: false};

  const chain = [];
  let p = hit, g = 0;
  while (p && g++ < 8) {
    const b = p.getBoundingClientRect();
    chain.push({
      tag: p.tagName,
      cls: (p.className || '').toString().slice(0, 110),
      id: p.id || '',
      testid: p.getAttribute('data-testid') || '',
      role: p.getAttribute('data-message-author-role')
            || p.getAttribute('data-role') || '',
      kids: p.children.length,
      area: Math.round(b.width * b.height),
      text: (p.innerText || '').trim().replace(/\\n/g, ' ').slice(0, 50)
    });
    p = p.parentElement;
  }

  // 该消息块内部的 HTML 结构（看有没有 markdown / 引用块特征）
  return {
    found: true,
    text: (hit.innerText || '').trim().slice(0, 200),
    html: hit.outerHTML.replace(/\\s+/g, ' ').slice(0, 700),
    chain: chain
  };
}"""


async def main() -> None:
    a = Doubao()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(2500)

    # 先问一句，等答案出来
    try:
        await a.new_chat(page)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(1200)
    inp, via = await a.find_input(page)
    print("输入框 via =", via)
    if inp:
        await a._type(page, inp, "郑州今天天气怎样？一句话。")
        await page.wait_for_timeout(300)
        await a.send(page, "")
        print("已发送，等 30s ...")
        await page.wait_for_timeout(30000)
    print("当前 URL:", page.url)

    # 顺便看各候选选择器能匹配到什么
    print("\n候选选择器匹配情况：")
    for sel in ["[class*='message-content']", "[class*='markdown-body']",
                "[class*='markdown']", "article", "[class*='answer']",
                "[class*='flow-markdown']", "[class*='md-']",
                "[data-testid]", "[class*='message']"]:
        try:
            n = await page.locator(sel).count()
        except Exception:
            n = -1
        print(f"   {sel:32} → {n}")

    r = await page.evaluate(JS)
    if not r.get("found"):
        print("没找到答案块 —— 可能页面已重置，先手动问一句再跑")
        await browser.manager.close_all()
        return
    print("\n答案文本:", repr(r["text"]))
    print("\n答案块 HTML:", r["html"][:500])
    print("\n祖先链（从答案块往上）：")
    for c in r["chain"]:
        print(f"  {c['tag']:6} kids={c['kids']:3} area={c['area']:7} "
              f"testid={c['testid'][:20]:20} role={c['role'][:12]:12}")
        print(f"         cls={c['cls'][:100]}")
        print(f"         text={c['text'][:48]!r}")
    await browser.manager.close_all()


asyncio.run(main())
