"""停机开关自测：验证「叫停之后程序真的不会再自己爬起来」。

浏览器崩了能自愈是好特性，但它的反面很糟 —— 用户想中止任务时，
怎么关它都要重新爬起来接着干。这个脚本验证五件事：

    1. 手动停机后，pool.ask 立即失败且**不开浏览器**
    2. <DATA_DIR>/STOP 文件哨兵同样生效（停机状态能扛住服务重启）
    3. ★ 删掉哨兵文件就等于恢复（不再需要调 /api/resume）
    4. 60 秒内浏览器被关 3 次 → 自动判定"用户想停"并停机
    5. resume 之后恢复正常
    6. ★ HTTP 直连（没有窗口的那条路）被叫停时，不许记成
       "直连失败 → 退回浏览器" —— 见 [3b]

用法：
    python tools/selftest_stop.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser, killswitch as ks, pool  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    flag = "✓" if cond else "✗"
    print(f"{flag} {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


async def main() -> int:
    # 起跑前必须是干净状态
    ks.resume()
    await browser.manager.close_all()

    # ------------------------------------------------------------ 1
    print("\n[1] 手动停机后调用应被拦截，且不开浏览器")
    ks.halt("自测停机")
    t0 = time.time()
    res = await pool.ask("一句话说清楚 PWM 和 DAC 的区别", provider="deepseek")
    el = time.time() - t0
    check("调用失败", not res.get("ok"), res.get("error", "")[:60])
    check("秒级返回（不是等满 180s）", el < 5.0, f"{el:.2f}s")
    check("错误里写明了怎么恢复", "resume" in (res.get("hint") or "")
          or "resume" in (res.get("error") or ""))
    check("浏览器始终没被拉起", not browser.manager.running())

    # ------------------------------------------------------------ 2
    print("\n[2] STOP 文件哨兵（模拟服务重启后仍然有效）")
    ks.resume()
    ks.STOP_FILE.write_text("手动放的哨兵", encoding="utf-8")
    ks.switch._file_checked_at = 0.0        # 跳过 0.5s 节流，立刻重读
    res2 = await pool.ask("再来一个测试问题", provider="deepseek")
    check("哨兵文件被识别", bool(ks.switch.halted()), ks.switch.halted())
    check("调用被拦截", not res2.get("ok"))

    # ------------------------------------------------------------ 2b
    print("\n[2b] ★ 删掉哨兵文件 = 恢复（不再需要 /api/resume）")
    ks.resume()
    ks.halt("自测：验证删文件即恢复")
    check("停机已生效", bool(ks.switch.halted()), ks.switch.halted()[:40])
    ks.STOP_FILE.unlink()
    ks.switch._file_checked_at = 0.0        # 跳过 0.5s 节流，立刻重读
    # 回归 2026-10-01 的 bug：以前 halted() 只处理"文件存在"，文件被删掉时
    # 从不 clear()，于是 rm 掉哨兵后仍一直报停机 —— 文档说"删掉即可恢复"，
    # 实际只有 /api/resume 能救。这里把它钉死。
    check("★ 删掉哨兵后 halted() 自己变空", not ks.switch.halted(),
          repr(ks.switch.halted())[:60])
    try:
        ks.switch.check()                   # 不再抛 HaltedError
        check("★ 删掉哨兵后 check() 不再抛错", True)
    except Exception as e:  # noqa: BLE001
        check("★ 删掉哨兵后 check() 不再抛错", False, f"{type(e).__name__}: {e}")
    check("恢复后哨兵文件确实不在了", not ks.STOP_FILE.exists())
    # 复用同一个进程继续验证：再停机一次仍然有效（标志位没被写坏）
    ks.halt("二进宫")
    ks.STOP_FILE.unlink()
    ks.switch._file_checked_at = 0.0
    check("★ 反复停机/删文件，判定依旧正确", not ks.switch.halted())

    # ------------------------------------------------------------ 3
    print("\n[3] 反复关闭浏览器应自动停机")
    ks.resume()
    for i in (1, 2, 3):
        stopped = ks.switch.note_rebuild()
        print(f"    第 {i} 次重建：{'已触发停机' if stopped else '继续观察'}")
    check("第 3 次触发自动停机", bool(ks.switch.halted()))
    check("写了哨兵文件", ks.STOP_FILE.exists())

    # ------------------------------------------------------------ 3b
    print("\n[3b] ★ HTTP 直连被叫停：不许记成「直连失败 → 退回浏览器」")
    # 背景（2026-10-01 真机实测）：deepseek 走直连问一句，7 秒后
    # POST /api/stop —— 流**确实**被中断了（SSE 循环里的 should_stop
    # 命中，连接当场断开），可日志写的是
    #     [http] 直连失败 → 退回浏览器：HttpError: [已停机] …
    # 通道什么都没坏，也没真去开浏览器 —— 是适配器那个
    # `except Exception` 把"用户叫停"吞成了"这条路走不通"。
    # 看日志的人会跑去查 HTTP 通道，而真正的原因只是他刚按了停机。
    from core.http_deepseek import HttpStopped

    class _FakeDS:                      # 只实现 _try_one 会用到的部分
        id = "deepseek"
        name = "假 DeepSeek（绝不起浏览器）"
        http_capable = True

        async def ask_http(self, *a, **kw):
            raise HttpStopped("[已停机] 用户在生成过程中叫停了本次调用")

    # 万一真的退回了浏览器，这里直接拦下，别让它去开 Playwright（7 秒）
    launched: list[str] = []
    _orig_ensure = browser.manager.ensure_page

    async def _no_launch(a, *args, **kwargs):
        launched.append(a.id)
        raise AssertionError("不该退回浏览器")

    browser.manager.ensure_page = _no_launch        # type: ignore[assignment]
    ks.resume()          # 场景是"生成到一半才叫停"，入口处还没停机
    try:
        r5 = await pool._try_one(_FakeDS(), "任意问题", reset=False, timeout=5)
    finally:
        browser.manager.ensure_page = _orig_ensure  # type: ignore[assignment]
    check("返回失败", not r5.get("ok"))
    check("错误就是停机原因原文", "已停机" in (r5.get("error") or ""),
          repr(r5.get("error"))[:64])
    check("★ 不是浏览器包裹后的 RuntimeError（那说明又退回去了）",
          "RuntimeError" not in (r5.get("error") or ""))
    check("★ 没有退回浏览器", not launched, f"launched={launched}")
    check("也没开任何窗口", not browser.manager.running())

    # 源码契约：这条路径靠三处"顺序"成立，任何一处被改回都会静默重演
    root = Path(__file__).resolve().parent.parent
    http_src = (root / "core/http_deepseek.py").read_text(encoding="utf-8")
    ad_src = (root / "core/adapter_deepseek.py").read_text(encoding="utf-8")
    pool_src = (root / "core/pool.py").read_text(encoding="utf-8")
    check("http_deepseek 定义了 HttpStopped", "class HttpStopped(" in http_src)
    check("中断时抛 HttpStopped，不再抛裸 HttpError",
          "raise HttpStopped(" in http_src
          and 'raise HttpError("[已停机]' not in http_src)
    # ★ 判据必须限定在 ask_http 那段 try 里再比先后：这个文件靠前的
    #   harvest() 里**也有一个** `except Exception as e:  # noqa: BLE001`，
    #   直接用全局 index() 会拿到那一个，于是断言假红 ——
    #   是自测自己写得不严谨，不是代码错（踩过一次，记在这里）。
    _k = ad_src.index("await http_deepseek.ask(")
    _ad_seg = ad_src[_k:]
    check("★ 适配器把 HttpStopped 原样抛出（排在 except Exception 之前）",
          _ad_seg.index("except http_deepseek.HttpStopped:")
          < _ad_seg.index("except Exception as e:  # noqa: BLE001"))
    _i = pool_src.index("should_stop=switch.halted)")
    _seg = pool_src[_i:_i + 3000]
    check("★ pool 接住 HttpStopped 并收手（排在 except Exception 之前）",
          _seg.index("except HttpStopped as e:")
          < _seg.index("except Exception as e:  # noqa: BLE001"))

    # ------------------------------------------------------------ 4
    print("\n[4] resume 之后恢复")
    ks.resume()
    check("停机状态已清", not ks.switch.halted())
    check("哨兵文件已删", not ks.STOP_FILE.exists())
    # 恢复后 ensure_page 应当能真的开起来（验证没有把正常路径一起打死）
    from core.adapters import get
    a = get("deepseek")
    try:
        page = await browser.manager.ensure_page(a)
        check("浏览器可以正常拉起", page is not None, f"URL={page.url[:48]}")
    except Exception as e:  # noqa: BLE001
        check("浏览器可以正常拉起", False, f"{type(e).__name__}: {e}")

    print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项：' + ', '.join(FAILS)}")
    return 0 if not FAILS else 1


async def _run() -> int:
    """★ 建浏览器和关浏览器必须在**同一个事件循环**里。

    原先写成 `asyncio.run(main())` + `finally: asyncio.run(close_all())` ——
    两个循环。Playwright 的子进程在第一个循环里拉起、在第二个循环里关闭，
    第一个循环的 subprocess transport 永远没人回收 →
    解释器退出时刷一屏 "unclosed transport" 的 ResourceWarning，
    打印 repr 时还会再抛 `ValueError: I/O operation on closed pipe`。
    它出现在"全部通过"之后，看着像测试炸了，实际只是收尾噪声 ——
    而这种噪声最坑人的地方是**把真的失败淹掉**。
    """
    try:
        return await main()
    finally:
        # 别把停机状态留给下一次运行
        ks.resume()
        await browser.manager.close_all()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
