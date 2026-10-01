"""生图抓取自测：验证「只取本轮新生成的图」，用本地假页面，不联网、不登录、不耗额度。

## 回归的是什么（2026-10-01 真机实测踩到）

`extract_images()` 原本是「把页面上所有 >= min_side 的 <img> 按面积排序取前 6」——
**没有"这轮的图"这个概念**。于是只要走"复用页面会话"（`reset=False`），
历史消息里还没滚走的旧图就会被当成这一轮的结果重新下载一遍。

真机证据：同一个豆包页面连续两次不同提示词（奶龙 / 橘猫），
抓回来的 4 张图 **SHA256 逐张完全相同**。而且返回值看起来完全正常，
`ok=true`、`images` 有 4 个 —— 属于最难发现的那类错。

修法：发问前先把页面上现有的图（`img_baseline`）记下来，抓图时排除掉。
★ 排除必须在 **JS 里 filter 之后**再 `slice(0, limit)`：
   先切片再排除的话，旧图一多就会把新图挤出 limit，表现为"这轮没生成图"。

## 为什么要自己造图

真站点要登录、要联网、还要等模型出图（10~40 秒），没法当回归用。
这里用 canvas → toBlob → objectURL 造出**真实可解码**的图（naturalWidth 是真的），
再把尺寸压低到 CSS 200px 模拟"缩略图"。

用法：
    python tools/selftest_grab_images.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser  # noqa: E402
from core.adapters import get  # noqa: E402

TOTAL = 0
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


# 造图脚本：返回每张图的 objectURL，并按 tag 分类
MAKE_IMAGES_JS = """async (spec) => {
    // spec: [{tag, size, n}] —— 造 n 张 size×size 的图，打上 tag
    const made = [];
    for (const group of spec) {
      for (let i = 0; i < group.n; i++) {
        const c = document.createElement('canvas');
        c.width = c.height = group.size;
        const g = c.getContext('2d');
        // 每张颜色不同，但**同一次调用内固定**，方便按 src 比对
        g.fillStyle = `hsl(${(group.size + i * 37) % 360},70%,60%)`;
        g.fillRect(0, 0, group.size, group.size);
        const blob = await new Promise(r => c.toBlob(r, 'image/png'));
        const url = URL.createObjectURL(blob);
        const img = document.createElement('img');
        img.src = url;
        img.dataset.tag = group.tag;
        // CSS 尺寸压小，模拟缩略图；naturalWidth 仍是 canvas 的真实尺寸
        img.style.width = '200px';
        img.style.height = '200px';
        document.body.appendChild(img);
        await img.decode();
        made.push({tag: group.tag, src: url, size: group.size});
      }
    }
    return made;
}"""

# data: 占位 + 过小图 + 隐藏图，都该被过滤掉
NOISE_HTML = """
  <img id="placeholder" src="data:image/png;base64,iVBORw0KGgo=">
  <img id="tiny" src="" style="width:10px;height:10px">
  <img id="hidden" style="display:none;width:200px;height:200px">
"""


async def main() -> int:
    a = get("deepseek")
    pw = await browser.async_playwright().start()
    channel = None
    try:
        from core import settings
        channel = (settings.get()["browser"]["channel"] or "").strip() or None
    except Exception:
        pass
    kwargs = {"headless": True}
    if channel:
        kwargs["channel"] = channel
    ctx = await pw.chromium.launch(**kwargs)
    page = await ctx.new_page()
    try:
        # ---------------- 1) 没有旧图时应当全取
        print("\n=== 无旧图：应当全部取回 ===")
        await page.set_content(f"<body>{NOISE_HTML}</body>")
        made = await page.evaluate(MAKE_IMAGES_JS,
                                   [{"tag": "new", "size": 800, "n": 3}])
        got = await a.extract_images(page)
        check("3 张图全取到", len(got) == 3, f"得到 {len(got)}")
        check("data: 占位图被排除",
              all("placeholder" not in s for s in got))
        check("返回的就是刚造的 objectURL",
              set(got) == {m["src"] for m in made})

        # ---------------- 2) ★ 核心回归：排除旧图（旧代码在这里会返回 0）
        #
        # 关键设计：**旧图要比新图大，而且数量要多于 limit(6)**。
        #   旧代码 = JS 先按面积取前 6（全是旧图）→ 再排除 → 结果 0
        #   新代码 = JS 先排除旧图 → 再取前 6 → 结果 2
        # 数量若不超过 limit，旧代码也能"碰巧"通过，就测不出这个 bug 了。
        print("\n=== 复用页面会话：旧图多且更大，只能取到新图 ===")
        await page.set_content(f"<body>{NOISE_HTML}</body>")
        old = await page.evaluate(MAKE_IMAGES_JS,
                                  [{"tag": "old", "size": 900, "n": 8}])
        baseline = set(await a.extract_images(page, limit=500))
        check("基线快照收全 8 张旧图", len(baseline) == 8, f"得到 {len(baseline)}")

        new = await page.evaluate(MAKE_IMAGES_JS,
                                  [{"tag": "new", "size": 700, "n": 2}])
        got = await a.extract_images(page, exclude=baseline)
        check("★ 只返回 2 张新图（旧代码这里会返回 0）",
              len(got) == 2, f"得到 {len(got)}")
        check("★ 返回的正是新图，一张旧图都没混进来",
              set(got) == {m["src"] for m in new})
        check("旧图确实还在 DOM 上（证明这个场景是真实的）",
              len(await a.extract_images(page, limit=500)) == 10)

        # ---------------- 3) 全是旧图 → 空，不能拿旧的充数
        print("\n=== 这轮没生成新图：必须返回空，不能拿旧的充数 ===")
        got = await a.extract_images(page, exclude=baseline | {m["src"] for m in new})
        check("★ 全被排除 → 返回空", got == [], f"得到 {len(got)}")

        # ---------------- 4) limit 仍然生效
        print("\n=== limit 仍然生效（拍基线要用大值，取值默认 6）===")
        await page.set_content(f"<body>{NOISE_HTML}</body>")
        await page.evaluate(MAKE_IMAGES_JS, [{"tag": "n", "size": 800, "n": 10}])
        check("默认 limit=6", len(await a.extract_images(page)) == 6)
        check("limit=3 时只取 3 张",
              len(await a.extract_images(page, limit=3)) == 3)
        check("limit=500 时收全 10 张",
              len(await a.extract_images(page, limit=500)) == 10)

        # ---------------- 5) min_side 过滤
        print("\n=== 小于 min_side 的图被排除（头像/图标不该被抓）===")
        await page.set_content(f"<body>{NOISE_HTML}</body>")
        await page.evaluate(MAKE_IMAGES_JS,
                            [{"tag": "big", "size": 800, "n": 1},
                             {"tag": "small", "size": 256, "n": 1}])
        got = await a.extract_images(page)
        check("只留下 800px 那张（256px 被挡）", len(got) == 1, f"得到 {len(got)}")
    finally:
        await ctx.close()
        await pw.stop()

    print()
    if FAILS:
        print(f"✗ {len(FAILS)}/{TOTAL} 项失败：{'、'.join(FAILS)}")
        return 1
    print(f"✓ 全部通过（{TOTAL} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
