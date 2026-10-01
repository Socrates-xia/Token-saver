"""用豆包生成图片，并把生成结果下载到本地。

用法（需先停掉 server.py）：
    python tools/grab_doubao_image.py "帮我生成一张奶龙的图片" [输出目录]

要点：
- 用**轮询**等图片出现（一出现就下载），不是固定采样固定时长。
- 过滤掉头像/图标：只认 naturalWidth >= 512 的图。
- 用页面内 fetch 取图 → 走浏览器上下文，绕开 CDN 的防盗链/签名限制。
"""
import asyncio
import base64
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_doubao import Doubao  # noqa: E402

# 取页面上"像生成结果"的图：够大、且不是 data: 占位图
PICK = """() => {
  const out = [];
  for (const e of document.querySelectorAll('img')) {
    if (!e.offsetParent) continue;
    const w = e.naturalWidth || 0;
    if (w < 512) continue;
    const src = e.currentSrc || e.src || '';
    if (!src || src.startsWith('data:')) continue;
    out.push({src: src, w: w, h: e.naturalHeight || 0});
  }
  out.sort((a, b) => b.w * b.h - a.w * a.h);
  return out;
}"""

# 在页面上下文里把图取成 dataURL（带上 cookie / referer，最不容易被 CDN 拦）
FETCH = """async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    const b = await r.blob();
    return await new Promise(res => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.onerror = () => res('');
      fr.readAsDataURL(b);
    });
  } catch (e) { return ''; }
}"""


async def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "帮我生成一张奶龙的图片"
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("..").resolve()

    a = Doubao()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(2500)
    try:
        await a.new_chat(page)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(1200)

    inp, _ = await a.find_input(page)
    if not inp:
        print("找不到输入框"); return
    await a._type(page, inp, prompt)
    await page.wait_for_timeout(400)
    await a.send(page, "")
    print(f"已发送：{prompt}\n轮询等图片出现（最多 150s）…")

    t0 = time.time()
    hit = None
    deadline = t0 + 150
    while time.time() < deadline:
        await page.wait_for_timeout(2500)
        imgs = await page.evaluate(PICK)
        if imgs:
            hit = imgs[0]
            print(f"t={time.time() - t0:.1f}s 检测到图片 {hit['w']}x{hit['h']}")
            break
        print(f"  t={time.time() - t0:.0f}s 还没出图…")

    if not hit:
        print("超时，没等到图片")
        await browser.screenshot(page, "doubao", "img-fail")
        await browser.manager.close_all()
        return

    # 稍等一下再取，避免拿到未加载完的中间态
    await page.wait_for_timeout(1500)

    # 优先用 Playwright 的 request API：它走独立网络栈，不受页面同源策略约束，
    # 并自动带上 context 的 cookie。页面内 fetch 会被 CDN 的 CORS 挡下（实测返回空）。
    blob, ext = b"", "png"
    try:
        resp = await page.request.get(
            hit["src"], headers={"Referer": "https://www.doubao.com/"})
        if resp.ok:
            blob = await resp.body()
            ct = (resp.headers.get("content-type") or "").lower()
            if "jpeg" in ct or "jpg" in ct:
                ext = "jpg"
            elif "webp" in ct:
                ext = "webp"
    except Exception as e:  # noqa: BLE001
        print("request.get 失败：", e)

    if not blob:
        # 兜底：页面内 fetch（部分 CDN 允许跨域读取）
        data_url = await page.evaluate(FETCH, hit["src"])
        m = re.match(r"data:image/(\w+);base64,(.*)", data_url or "", re.S)
        if m:
            ext = m.group(1).replace("jpeg", "jpg")
            blob = base64.b64decode(m.group(2))
    if not blob:
        print("取图失败（两条路径都拿不到字节）")
        await browser.screenshot(page, "doubao", "img-fail")
        await browser.manager.close_all()
        return

    name = f"豆包生成_{int(time.time())}.{ext}"
    path = out_dir / name
    path.write_bytes(blob)
    print(f"已保存 {path}  ({len(blob) / 1024:.1f} KB)")
    print(f"总耗时 {time.time() - t0:.1f}s")

    await browser.screenshot(page, "doubao", "img")
    await browser.manager.close_all()


asyncio.run(main())
