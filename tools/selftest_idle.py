"""离线回归测试：空闲自动关闭窗口（BrowserManager.close_idle）。

    python tools/selftest_idle.py

不联网、不开浏览器、不消耗账号额度 —— 只构造真的 `BrowserManager`，
把 `_ctx` / `_page` 塞成假句柄（这正是生产代码自己往里写的位置）。

为什么要专门测：这个功能的失败方式是**两头的**，而且都不报错。

- 关早了 → 正跑着的那一轮提问手上的 page 被抽走，报一个跟原因毫无关系的
  `TargetClosedError`；用户看到的是"随机失败"。
- 关晚了 / 永不关 → 用户回到最初那个抱怨："每次调用完还得我手动关网页"。

所以三条安全线（持锁不关 / 无记录不关 / 0 即关闭）各来一条断言，
外加"正在提问的站点不动"这条最重要的。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.browser import BrowserManager  # noqa: E402

fails: list[str] = []
total = 0


def check(name: str, got, want) -> None:
    global total
    total += 1
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {name}")
    if not ok:
        print(f"      期望 {want!r}")
        print(f"      实际 {got!r}")
        fails.append(name)


class FakeCtx:
    """只实现被用到的那一个方法（close 是 await 的）。"""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def seat(mgr: BrowserManager, pid: str) -> FakeCtx:
    """把某站点摆成"窗口开着"的样子 —— 位置与生产代码一致。"""
    ctx = FakeCtx()
    mgr._ctx[pid] = ctx            # type: ignore[assignment]
    mgr._page[pid] = object()      # type: ignore[assignment]
    return ctx


async def main() -> int:
    print("=== 空闲自动关闭：三条安全线 ===")

    # --- ① 还没到点：不动 ---
    mgr = BrowserManager()
    ctx = seat(mgr, "doubao")
    mgr.mark_use("doubao")
    check("刚用过 → 不关", await mgr.close_idle(180), [])
    check("窗口还在", ctx.closed, False)

    # --- ② 到点了：关掉 ---
    mgr = BrowserManager()
    ctx = seat(mgr, "doubao")
    mgr.mark_use("doubao")
    mgr._last_use["doubao"] = time.time() - 999      # 假装闲置了很久
    check("闲置超时 → 关掉", await mgr.close_idle(180), ["doubao"])
    check("句柄真的 close 了", ctx.closed, True)
    check("缓存里已移除", "doubao" in mgr._ctx, False)
    check("记下了自动关闭时间（控制台可见）", "doubao" in mgr._idle_closed, True)
    check("计时条目被清掉（重开时重新起算）", "doubao" in mgr._last_use, False)

    # --- ③ ★ 最重要的一条：正在提问的站点绝不关 ---
    # 一次提问全程持有该站点的 per-site 锁；巡检在锁外动手就等于把别人手上的
    # page 抽走（表现为随机的 TargetClosedError）。
    mgr = BrowserManager()
    ctx = seat(mgr, "yuanbao")
    mgr.mark_use("yuanbao")
    mgr._last_use["yuanbao"] = time.time() - 999
    lock = mgr.lock("yuanbao")
    await lock.acquire()
    try:
        check("★ 持锁（正在提问）→ 坚决不关", await mgr.close_idle(180), [])
        check("★ 窗口完好", ctx.closed, False)
    finally:
        lock.release()
    check("锁放开后下一轮才关", await mgr.close_idle(180), ["yuanbao"])

    # --- ④ 只读锁状态，不许 await 拿锁（否则巡检会卡在第一家上）---
    mgr = BrowserManager()
    p = seat(mgr, "kimi")
    other = seat(mgr, "doubao")
    mgr._last_use["kimi"] = 0.0
    mgr._last_use["doubao"] = 0.0
    hold = mgr.lock("kimi")
    await hold.acquire()
    try:
        got = await asyncio.wait_for(mgr.close_idle(180), timeout=2)
    except asyncio.TimeoutError:
        got = "巡检卡住了"
    finally:
        hold.release()
    check("一家在忙不影响关另一家（且巡检不会卡住）", got, ["doubao"])
    check("被跳过的那家窗口留着", p.closed, False)
    check("另一家关了", other.closed, True)

    # --- ⑤ 没有计时记录的：先记成"刚用过"，不关 ---
    mgr = BrowserManager()
    ctx = seat(mgr, "tongyi")
    check("无记录 → 本轮不关", await mgr.close_idle(180), [])
    check("但补上了计时（下一轮才可能关）", "tongyi" in mgr._last_use, True)

    # --- ⑥ 0 = 整个功能关掉 ---
    mgr = BrowserManager()
    ctx = seat(mgr, "doubao")
    mgr._last_use["doubao"] = 0.0
    check("idle_close_seconds=0 → 不关（功能关闭）", await mgr.close_idle(0), [])
    check("窗口一直留着", ctx.closed, False)

    # --- ⑦ mark_use 会续计时（"还在追问就别关"就是靠这个）---
    mgr = BrowserManager()
    ctx = seat(mgr, "doubao")
    mgr._last_use["doubao"] = time.time() - 999
    mgr.mark_use("doubao")
    check("追问刷新计时后 → 又变成不该关", await mgr.close_idle(180), [])

    print()
    print("=== 源码契约（防止有人'顺手简化'掉关键细节）===")
    br = (ROOT / "core" / "browser.py").read_text(encoding="utf-8")
    sv = (ROOT / "server.py").read_text(encoding="utf-8")
    po = (ROOT / "core" / "pool.py").read_text(encoding="utf-8")
    st = (ROOT / "core" / "settings.py").read_text(encoding="utf-8")

    check("巡检只读锁状态，不 await 拿锁", "lock.locked()" in br, True)
    check("服务启动时挂上巡检任务", "create_task(_idle_watchdog())" in sv, True)
    check("服务退出时取消巡检", "idle_task.cancel()" in sv, True)
    check("有关闭窗口但不停服务的接口", '@app.post("/api/close")' in sv, True)
    check("默认值是 180 秒", '"idle_close_seconds": 180' in st, True)
    check("问完才刷新计时（在 _try_one 收尾处）",
          "browser.manager.mark_use(a.id)" in po, True)
    # HTTP 直连路线在上面就 return 了，不该替一个闲置窗口续命
    check("HTTP 直连路线不刷新计时（它压根没开浏览器）",
          po.index("browser.manager.mark_use(a.id)")
          > po.index('"_via_http": True'),
          True)

    print()
    if fails:
        print(f"✗ {len(fails)} 项失败：{'、'.join(fails)}")
        return 1
    print(f"✓ 全部通过（{total} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
