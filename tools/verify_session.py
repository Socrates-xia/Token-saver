"""一次性验证：登录后凭证能否跨浏览器重启存活。

    python tools/verify_session.py deepseek

各家站点几乎都把会话凭证放在 session cookie 里（DeepSeek 的 ds_session_id
就是 persistent=0），浏览器一关就被浏览器自身丢弃 —— 于是每次重启都要重新
扫码。这里验证 save/restore 这条路走得通：登录 → 存盘 → 关浏览器 → 重开 →
看还认不认得你。

同时也用于"重新登录"：登录成功后顺手就把凭证存好了，之后不必再跑它。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser  # noqa: E402
from core.adapters import get, ids  # noqa: E402


async def main(pid: str, wait: float) -> int:
    a = get(pid)
    if a is None:
        print(f"未知站点：{pid}（可用：{', '.join(ids())}）")
        return 2

    page = await browser.manager.ensure_page(a)
    if await a.is_logged_in(page):
        print(f"✓ {a.name} 当前已登录，无需扫码。")
    else:
        print(f"\n★ 请在弹出的浏览器里登录 {a.name}，最多等 {int(wait)} 秒。\n")
        if not await a.wait_for_login(page, timeout=wait):
            print("✗ 等待登录超时，凭证没能存下来。下次调用仍需登录。")
            return 1

    saved = await browser.manager.save_session(a.id)
    if not saved:
        print("✗ 凭证存盘失败")
        return 1
    print(f"✓ 凭证已存盘：{saved}")

    # ---- 关键一步：彻底关掉浏览器，模拟真实重启
    print("\n关闭浏览器，模拟重启……")
    await browser.manager.close_all()
    await asyncio.sleep(2)

    print("重新打开……")
    page2 = await browser.manager.ensure_page(a)
    ok = await a.is_logged_in(page2)
    print(f"\n{'✓' if ok else '✗'} 重启后登录态：{'仍然在线' if ok else '已丢失'}"
          f"  （URL: {page2.url[:60]}）")
    if ok:
        print("\n以后再关掉浏览器也不用重新扫码了 —— 每次提问会自动刷新凭证。")
    else:
        print("\n仍然丢失。可能原因：该站点服务端把 token 判为过期，"
              "或登录态存在 localStorage 而非 cookie（这时换 wait_for_login "
              "后的 storage_state 导出看看）。")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("provider", nargs="?", default="deepseek")
    ap.add_argument("--wait", type=float, default=240.0)
    args = ap.parse_args()
    try:
        rc = asyncio.run(main(args.provider, args.wait))
    finally:
        asyncio.run(browser.manager.close_all())
    sys.exit(rc)
