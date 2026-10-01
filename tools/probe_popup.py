"""弹窗勘查器：实地打开某家网页端，报告有没有浮层挡路、能不能关掉。

用法：
    python tools/probe_popup.py deepseek
    python tools/probe_popup.py deepseek --shoot     # 额外存一张截图

站点改版后某家突然「点不动输入框」，先跑它：多数情况是浮层压住了输入框，
而报错信息里完全看不出来。输出会告诉你该往 config.yaml 的
providers.<id>.selectors.popup_close 里补什么。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser, settings  # noqa: E402
from core.adapters import get, ids  # noqa: E402

POPUP_ROOTS = [
    "[role='dialog']", "[aria-modal='true']",
    "[class*='modal' i]", "[class*='dialog' i]",
    "[class*='popup' i]", "[class*='drawer' i]",
    "[class*='overlay' i]", "[class*='update-log' i]",
    "[class*='announcement' i]",
]


async def main(pid: str, shoot: bool) -> int:
    a = get(pid)
    if not a:
        print(f"未知站点：{pid}\n可用：{', '.join(ids())}")
        return 2

    page = await browser.manager.ensure_page(a)
    await page.goto(a.url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(4000)   # 有些浮层是延迟几秒才弹的

    print(f"\n=== {a.name} ({pid}) ===")
    print(f"页面标题：{await page.title()}")
    print(f"当前 URL ：{page.url}")

    logged = await a.is_logged_in(page)
    print(f"登录状态：{'已登录' if logged else '★ 未登录（浮层多半是登录框）'}")

    found = await page.evaluate(
        """(roots) => {
          const out = [];
          for (const rs of roots) {
            document.querySelectorAll(rs).forEach(e => {
              const r = e.getBoundingClientRect();
              const cs = getComputedStyle(e);
              if (!r.width || !r.height) return;
              if (cs.visibility === 'hidden' || cs.display === 'none') return;
              const btns = [];
              e.querySelectorAll('button, [role="button"], a').forEach(b => {
                const br = b.getBoundingClientRect();
                if (!br.width || !br.height) return;
                btns.push({
                  text: (b.innerText || '').trim().slice(0, 16),
                  aria: b.getAttribute('aria-label') || '',
                  cls: (b.className || '').toString().slice(0, 60)
                });
              });
              out.push({
                sel: rs, cls: (e.className || '').toString().slice(0, 90),
                area: Math.round(r.width * r.height), z: cs.zIndex,
                text: (e.innerText || '').trim().slice(0, 150),
                buttons: btns.slice(0, 10)
              });
            });
          }
          return out;
        }""", POPUP_ROOTS)

    # 面积太小的同名元素不是浮层，滤掉再看
    real = [f for f in found if f["area"] >= 40000]
    print(f"\n命中浮层容器 {len(found)} 个，其中够大得像浮层的 {len(real)} 个：")
    for f in real:
        print(f"  · [{f['sel']}] cls={f['cls']}")
        print(f"    面积={f['area']}  z-index={f['z']}")
        if f["text"]:
            print(f"    文字：{f['text'][:100]}")
        for b in f["buttons"]:
            tag = b["aria"] or b["text"] or b["cls"]
            print(f"      ↳ 可点：<{b['cls']}> “{tag}”")
    if not real:
        print("  （没有 —— 页面上没有挡路的浮层）")

    print(f"\n程序判断 _has_popup()：{await a._has_popup(page)}")

    if shoot:
        shot = await browser.screenshot(page, pid, "popup")
        print(f"截图：{shot}")

    print("\n=== 试着清理 ===")
    closed = await a.dismiss_popups(page)
    left = await a._has_popup(page)
    print(f"已关闭：{closed or '（无）'}")
    print(f"清理后仍有浮层：{left}")
    if left:
        print("\n没清掉。把上面列出的容器写进 config.yaml：")
        print(f"providers:\n  {pid}:\n    selectors:\n      popup_close:")
        for f in real:
            print(f"        - \"[{f['cls'].split()[0]} 填这里] [class*='close' i]\"")
            break
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("provider")
    ap.add_argument("--shoot", action="store_true", help="额外保存截图")
    args = ap.parse_args()
    try:
        rc = asyncio.run(main(args.provider, args.shoot))
    finally:
        asyncio.run(browser.manager.close_all())
    sys.exit(rc)
