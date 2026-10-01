"""弹窗清理自测：用本地假页面验证 dismiss_popups 的逻辑，不依赖登录态。

真站点要登录才弹得出浮层，没法随时复现；这里手工造四种典型浮层，
逐个验证能不能关掉。站点改版后改动 _tag_close_candidates / dismiss_popups，
先跑它确认没把其它三类的能力改坏。

用法：
    python tools/selftest_popup.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser  # noqa: E402
from core.adapters import get  # noqa: E402

CASES: dict[str, str] = {
    # 1) 标准 ARIA 弹窗：容器内是带 aria-label 的图标关闭键 —— Esc 与选择器都能命中
    "aria_dialog": """
      <div role="dialog" aria-modal="true" id="p"
           style="position:fixed;inset:0;width:100vw;height:100vh;background:rgba(0,0,0,.5)">
        <div style="width:300px;height:200px;background:#fff;margin:100px auto">
          <span id="txt">更新日志</span>
          <button aria-label="close" id="x">×</button>
        </div>
      </div>""",

    # 2) 只有中文文案的确认键：命中"我知道了"，没有 aria
    "chinese_text": """
      <div class="xx-modal" id="p"
           style="position:fixed;left:0;top:0;width:100vw;height:100vh;background:gray">
        <div style="width:300px;height:200px;background:#fff;margin:100px auto">
          <p>新功能介绍</p>
          <button id="x">我知道了</button>
        </div>
      </div>""",

    # 3) 最刁钻：无文字无 aria，只有 class 里带 close，且 Esc 不响应。
    #    考验"class*='close' + 站点选择器 / 遮罩点击"这条兜底链路
    "silent_icon": """
      <div class="ds-modal-wrapper" id="p"
           style="position:fixed;left:0;top:0;width:100vw;height:100vh;background:rgba(9,9,9,.6)">
        <div style="width:300px;height:200px;background:#fff;margin:100px auto">
          <p>服务条款更新</p>
          <div class="modal-close-icon" id="x"
               style="width:20px;height:20px;background:#ccc;cursor:pointer"></div>
        </div>
      </div>""",

    # 4) 监听 Esc 关闭的遮罩，但内部没有任何"看起来像关闭"的元素。
    #    只有第一条 Esc 分支能救，用来验证 Esc 确实在跑
    "esc_only": """
      <div class="overlay-layer" id="p"
           style="position:fixed;left:0;top:0;width:100vw;height:100vh;background:rgba(0,0,0,.4)">
        <div style="width:300px;height:200px;background:#fff;margin:100px auto">
          <p>按 Esc 关闭我</p>
        </div>
      </div>""",
}

PAGE = """<!doctype html><html><body>
<textarea id="chat-input" placeholder="给 DeepSeek 发送消息"></textarea>
<button>发送</button>
%s
<script>
  const p = document.getElementById('p');
  if (p && p.className === 'overlay-layer') {
    // esc_only：只认 Esc
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') p.remove();
    });
  } else if (p) {
    const x = document.getElementById('x');
    if (x) x.addEventListener('click', () => p.remove());
  }
</script>
</body></html>"""


async def main() -> int:
    a = get("deepseek")
    page = await browser.manager.ensure_page(a)
    tmp = Path(tempfile.mkdtemp())
    passed = failed = 0

    for name, popup_html in CASES.items():
        f = tmp / f"{name}.html"
        f.write_text(PAGE % popup_html, encoding="utf-8")
        await page.goto(f.as_uri(), wait_until="domcontentloaded")
        await page.wait_for_timeout(500)

        before = await a._has_popup(page)
        closed = await a.dismiss_popups(page)
        after = await a._has_popup(page)
        # input 是否恢复可点（这才是最终目的）
        try:
            await page.locator("#chat-input").click(timeout=2500)
            clickable = True
        except Exception:
            clickable = False

        ok = before and not after and clickable
        if ok:
            passed += 1
        else:
            failed += 1
        flag = "✓" if ok else "✗"
        print(f"{flag} {name:14s} 检测到={before} 清理后={after} "
              f"输入框可点={clickable}  方式={closed or '—'}")

    print(f"\n通过 {passed} / {len(CASES)}")
    return 0 if failed == 0 else 1


async def _run() -> int:
    """★ 建浏览器和关浏览器必须在**同一个事件循环**里。

    原先写成 `asyncio.run(main())` + `finally: asyncio.run(close_all())` ——
    两个循环。Playwright 的子进程是在第一个循环里拉起来的，
    却在第二个循环里关，第一个循环的 subprocess transport 永远没人回收。
    结果是解释器退出时刷一屏 asyncio 的 ResourceWarning
    （"unclosed transport"），而且打印 repr 时还会再抛一个
    `ValueError: I/O operation on closed pipe`。
    它出现在"全部通过"之后，看起来像测试炸了，实际只是收尾噪声 ——
    这种噪声最坑人的地方是**把真的失败淹掉**。
    """
    try:
        return await main()
    finally:
        await browser.manager.close_all()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
