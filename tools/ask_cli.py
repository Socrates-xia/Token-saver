"""命令行直接外包一次任务，不必起服务。

    python tools/ask_cli.py deepseek "你的问题"
    python tools/ask_cli.py doubao "画一只奶龙" --grab-images
    python tools/ask_cli.py kimi "继续追问" --continue        # 接着上一轮在同一个会话里问
    python tools/ask_cli.py deepseek "x" --no-mode            # 不切深度思考

未登录会**打开浏览器等你扫码**，登录后自动继续，不用重跑命令。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser, settings  # noqa: E402
from core.adapters import get, ids  # noqa: E402
from core.adapter import strip_noise  # noqa: E402


async def ensure_login(a, wait_sec: float) -> bool:
    if await a.is_logged_in(await browser.manager.ensure_page(a)):
        return True
    page = await browser.manager.ensure_page(a)
    try:
        await page.goto(a.url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:  # noqa: BLE001
        print(f"打开页面失败：{e}")
    print(f"\n★ {a.name} 尚未登录 —— 请在刚弹出的浏览器里扫码/手机号登录。")
    print(f"  最多等 {int(wait_sec)} 秒，登录后会自动继续，不用重跑命令。\n")
    ok = await a.wait_for_login(page, timeout=wait_sec)
    if not ok:
        print(f"✗ 等待登录超时（{int(wait_sec)}s）")
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser(description="把任务外包给免费网页端 AI")
    ap.add_argument("provider", choices=ids(), help="站点 id")
    ap.add_argument("prompt", help="要问的内容")
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="接着上一轮聊（同一会话里追问，不重新发历史）")
    ap.add_argument("--no-mode", action="store_true",
                    help="不自动切深度思考/模型（生图类请求必须用）")
    ap.add_argument("--grab-images", action="store_true",
                    help="回答里有图时把图也下载到本地")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--login-wait", type=float, default=180.0,
                    help="等待扫码登录的秒数")
    args = ap.parse_args()

    a = get(args.provider)
    if not a.enabled():
        print(f"✗ {a.name} 在 config.yaml 里被关掉了（enabled: false）")
        return 2

    if not await ensure_login(a, args.login_wait):
        return 1

    if args.grab_images and args.provider != "doubao":
        print("注意：生图目前只在豆包上适配过，其他站点大概率抓不到图。")

    print(f"\n=== {a.name} 回答 ===\n")
    try:
        res = await a.ask(await browser.manager.ensure_page(a), args.prompt,
                          reset=not args.cont, timeout=args.timeout,
                          no_mode=args.no_mode, grab_images=args.grab_images)
    except Exception as e:  # noqa: BLE001
        print(f"✗ {type(e).__name__}: {e}")
        return 1

    if not res.ok:
        print(f"✗ 失败：{res.error}")
        if res.via:
            print(f"  轨迹：{res.via}")
        return 1

    # ★ 必须走 strip_noise：引用角标（-11 这类）、"参考 6 篇资料"状态行、
    #   以及工具栏标签都靠它清。pool.ask 内部会做，但这里是直接调 adapter，
    #   绕过了那一层 —— 不清的话答案里会带着 "-11" 之类的来源角标。
    print(strip_noise(res.answer))
    print(f"\n--- 用时 {res.elapsed:.1f}s  轨迹：{res.via}")
    if res.mode:
        print(f"    模式：{res.mode}")
    if res.images:
        print(f"    图片 {len(res.images)} 张：")
        for p in res.images:
            print(f"      {p}")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已取消")
        rc = 130
    finally:
        asyncio.run(browser.manager.close_all())
    sys.exit(rc)
