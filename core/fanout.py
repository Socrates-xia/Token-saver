"""并行外包：把一个任务拆成多部分，同时丢给多家网页端 AI。

为什么值得做
------------
token-saver 之前是"一家一家问"：N 个部分就是 N 段串行等待。
但每家站点都是**独立的浏览器上下文**（各自一个 persistent context + page），
彼此之间没有任何共享状态，天然可以并行。实测每家 20~40 秒，
4 家并行就能把 4 份活的总墙钟时间压到接近 1 份。

已有的并发基础（不用改）
------------------------
- `pool.ask` 里**没有全局锁**，不同 provider 可以同时在跑；
- `_try_one` 里有 `async with browser.manager.lock(a.id)`，
  **同一家**会自动串行 —— 所以"一条车道里排多个部分"是安全的，
  不会有两个部分挤进同一个网页会话；
- 纯 HTTP 路线（DeepSeek）在拿锁**之前**就 return 了，
  所以它压根不占车道，甚至同一家跑多个部分也不冲突。

车道模型
--------
    部分1 ─→ 车道A（元宝）   ─┐
    部分2 ─→ 车道B（豆包）    │
    部分3 ─→ 车道C（DeepSeek）├─ asyncio.gather ─→ 按编号合并
    部分4 ─→ 谁先空谁领       │
    部分5 ─→ …               ─┘

**默认"动态抢夺"**：不预先分配，所有车道从一个共享队列里领活，
谁先做完谁领下一个。真机实测两家的速度差 10 倍以上
（DeepSeek 纯 HTTP 只要 1.3~2.6 秒，元宝/豆包要开浏览器、20~28 秒），
预先平均分会让快车道早早干完、晾在旁边看慢车道干活 ——
实测 6 段/3 车道只有 1.87x；动态抢夺总时间能压到接近"最慢的那一段"。
传 `schedule="roundrobin"` 可以反过来刻意均摊（用于避免集中消耗某一家额度）。

同一条车道内的多个部分**串行**执行 —— 这不是性能妥协，而是正确性要求：
一个站点只有一个页面，两个部分同时打进去会把网页端会话搅乱
（`_try_one` 里 `browser.manager.lock(a.id)` 也会把它们排成一队）。
想提高并发就加车道（多登录几家站点），而不是给同一家加压。
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from . import adapters as provider_pkg
from . import browser, pool, router, settings

# 车道数上限。每条车道 = 一个可见浏览器窗口，开太多会拖慢机器、
# 也更容易被站点风控盯上。
_DEFAULT_MAX_LANES = 4


def _has_saved_login(pid: str) -> bool:
    """这个站点有没有落盘的登录态。

    用来筛车道候选：没登录过的站点（比如本机就没配过的 kimi）
    拉起来也只会停在登录页，白白占一条车道、还让用户看到多余窗口。
    """
    return (settings.DATA_DIR / "state" / f"{pid}.json").exists()


async def _pick_lanes(providers: list[str] | None, prompt: str,
                      max_lanes: int) -> tuple[list, list[str]]:
    """选出这次要用哪几家做车道。返回 `(车道, 被跳过的站点 id)`。

    指定了 providers 就按指定的来（顺便帮用户发现写错的名字）；
    没指定就按"有登录态 + 路由器打分"排。

    ★ 显式点名**同样要尊重 `enabled`**。以前这里直接用 `by_id[p]`，
    于是 config.yaml 里写着 `enabled: false` 的站点照样会被拉起来 ——
    "我明明在配置里关了它"却还是弹出了窗口，这种**静默违反配置**最难排查
    （没有任何报错，只是多出一个窗口、多耗一份额度）。
    现在跳过它们并如实回报，但**不硬失败**：调用方常常把"所有站点"
    一股脑列进 providers，硬报错反而更难用。
    """
    all_a = provider_pkg.all_adapters()
    enabled = [a for a in all_a if a.enabled()]
    if providers:
        by_id = {a.id: a for a in all_a}
        missing = [p for p in providers if p not in by_id]
        if missing:
            raise ValueError(
                f"未知 provider: {', '.join(missing)}；"
                f"可用的是 {', '.join(a.id for a in all_a)}")
        lanes, skipped = [], []
        for p in providers:
            a = by_id[p]
            if a.enabled():
                lanes.append(a)
            else:
                skipped.append(p)
        return lanes[:max_lanes], skipped

    running = set(browser.manager.running())
    cands = [a for a in enabled if _has_saved_login(a.id) or a.id in running]
    if not cands:
        # 一个都没登录过 → 退回"全部启用的"，让它们在提问时自己去登
        cands = enabled
    avail = await pool.availability()
    return router.rank(cands, avail, prompt)[:max_lanes], []


def _build_prompt(template: str, part: str, n: int, total: int) -> str:
    """把模板和这一部分拼起来。

    支持 {part} / {n} / {total} 三个占位符。没写 {part} 时把 part 追加在末尾
    —— 这是最不容易出错的兜底：用户写了模板却忘了占位符时，
    内容仍然会被带上，而不是被静默丢弃。
    """
    if not template:
        return part
    out = template
    if "{total}" in out:
        out = out.replace("{total}", str(total))
    if "{n}" in out:
        out = out.replace("{n}", str(n))
    if "{part}" in out:
        return out.replace("{part}", part)
    return out.rstrip() + "\n\n" + part


def _merge(results: list[dict], merge: str, label: str) -> str:
    """按**部分编号**合并，绝不依赖列表顺序。

    并行跑完的完成顺序是乱的，而调用方拿到的 `results` 只要有一次不是按
    原始下标构造（比如以后有人图省事改成 `append`），合并稿的段落顺序就会
    静默错乱 —— 用户拿到的是一篇被打乱的文章，而且看不出哪里不对。
    所以这里自己按 `n` 排一遍，不把这个契约押在调用方身上。
    """
    ok = [r for r in results
          if r.get("ok") and (r.get("answer") or "").strip()]
    ok.sort(key=lambda r: r.get("n", 0))
    if merge == "json":
        return ""
    if merge == "concat":
        return "\n\n".join(r["answer"].strip() for r in ok)
    blocks = []
    for r in ok:
        try:
            head = label.format(n=r["n"], total=r["total"])
        except Exception as e:  # noqa: BLE001
            # 兜底还在，但**不再静默**：`total` 曾经没被 do_part 写进结果里，
            # label 于是一直喂默认值（"第N部分"），用户传的 label 形同虚设，
            # 而这条 except 把 KeyError 一口吞掉、什么都没留下。
            print(f"[fanout] label 格式化失败，退回默认标题：{type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            head = f"第{r['n']}部分"
        blocks.append(f"## {head}\n\n{r['answer'].strip()}")
    return "\n\n---\n\n".join(blocks)


def _part_record(n: int, total: int, provider: str, provider_name: str,
                 r: dict, elapsed: float, via_suffix: str = "") -> dict:
    """构造"单个部分"的结果记录。

    ★ 这个字典的**形状**是 `_merge`、`parts` 返回值和上层调用方共用的契约
    （尤其 `total`：`label` 的 `{total}` 占位符要用它）。抽成一个函数是为了
    别再出现"生产代码少写一个键、测试夹具手写又补上"这种两边不一致的假绿 ——
    2026-10-01 就是这么栽的：`do_part` 从来没写过 `total`，于是
    `_merge` 里 `label.format(...)` 每次抛 KeyError、被 except 吞掉，
    调用方传的 `label` 完全失效，而 `selftest_fanout.py` 因为夹具里手写了
    `total` 一直是绿的。
    """
    return {
        "n": n,
        "total": total,
        "provider": provider,
        "provider_name": provider_name,
        "ok": bool(r.get("ok")),
        "answer": r.get("answer") or "",
        "error": r.get("error") or "",
        "via": (r.get("via") or "") + via_suffix,
        "elapsed": round(elapsed, 2),
    }


async def fanout(parts: list[str], *, template: str = "",
                 providers: list[str] | None = None,
                 timeout: float | None = None,
                 merge: str = "sections",
                 label: str = "第{n}部分",
                 max_lanes: int | None = None,
                 schedule: str = "dynamic",
                 retry_failed: bool = True) -> dict[str, Any]:
    """把 parts 并行分派到多家站点，返回逐部分结果 + 合并稿。

    **各部分互不共享上文**：每个部分都是全新会话（强制 reset=True），
    所以拆出来的部分必须彼此独立 —— 需要"带着前文"的拆法不要用 fan-out，
    那种情况用 thread 串行追问。
    """
    parts = [p for p in (parts or []) if str(p).strip()]
    if not parts:
        return {"ok": False, "error": "parts 不能为空"}

    rt = settings.get()["runtime"]
    if timeout is None:
        timeout = float(rt["ask_timeout"])
    if max_lanes is None:
        max_lanes = int(rt.get("fanout_max_lanes", _DEFAULT_MAX_LANES))
    max_lanes = max(1, min(int(max_lanes), len(parts)))

    # 校验而不是"不认识就走默认"：有人写了 "roundrobin "（多一个空格）时，
    # 静默退回 dynamic 会让"我要刻意均摊到各家"的意图落空 —— 这类悄悄
    # 改变行为的默认值最难发现。宁可报错让他看见可用取值。
    schedule = (schedule or "dynamic").strip().lower()
    if schedule not in ("dynamic", "roundrobin"):
        return {"ok": False,
                "error": f"未知 schedule: {schedule!r}；可用：dynamic / roundrobin"}

    try:
        lanes, disabled_skipped = await _pick_lanes(
            providers, _build_prompt(template, parts[0], 1, len(parts)), max_lanes)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not lanes:
        extra = ""
        if disabled_skipped:
            extra = ("；这些站点在 config.yaml 里是 enabled: false，已按配置跳过："
                     + ", ".join(disabled_skipped))
        return {"ok": False,
                "error": f"没有可用的 provider（都未启用或未登录？）{extra}"}

    t0 = time.time()
    results: list[dict] = [{} for _ in parts]
    taken: dict[str, list[int]] = {a.id: [] for a in lanes}

    # ---- 取活：动态抢夺 vs 轮转 ----
    #
    # ★ 默认用动态抢夺，而不是开跑前把部分平均分好。
    #   原因在真机实测里很直白（2026-10-01）：DeepSeek 走纯 HTTP 直连，
    #   一段译文 1.3~2.6 秒；元宝/豆包要开浏览器，一段 20~28 秒 —— **差 10 倍以上**。
    #   按轮转平均分，快的车道两下就干完了，剩下一半时间在旁边看慢车道干活，
    #   实测 6 段/3 车道只有 1.87x。
    #   改成"谁空谁领下一个"之后，快的自然多领，总时间被压到接近
    #   「最慢那一段」而不是「最慢车道的总时长」。
    #   想反过来（刻意均摊到各家、避免集中消耗某一家额度）就传 schedule="roundrobin"。
    if schedule == "roundrobin":
        buckets: list[list[int]] = [[] for _ in lanes]
        for i in range(len(parts)):
            buckets[i % len(lanes)].append(i)
    else:
        buckets = None
    queue: asyncio.Queue[int] = asyncio.Queue()
    if buckets is None:
        for i in range(len(parts)):
            queue.put_nowait(i)

    async def do_part(lane, i: int) -> None:
        n = i + 1
        prompt = _build_prompt(template, parts[i], n, len(parts))
        p0 = time.time()
        try:
            # ★ reset=True 必须传！
            #   不传的话 ask() 会把 tid 兜底成"该站点的隐式续聊话题"
            #   （auto-<provider>），于是**同一家的多个部分会被当成同一个
            #   话题的连续追问** —— 第二个部分白白把第一个部分的答案当历史
            #   带上（实测回填了 2264 字节），既费 token 又有串扰风险。
            #   reset=True 时 tid 为空 → 每次都是干净的一次性会话。
            #
            # fallback=False：让失败**留在本车道**、如实报出来。开着 fallback
            #   的话某个部分可能被甩到别的车道，那边正忙 → 一起排队，
            #   反而看不出是哪家的问题。
            r = await pool.ask(prompt, provider=lane.id, timeout=timeout,
                               reset=True, fallback=False)
            results[i] = _part_record(n, len(parts), lane.id, lane.name, r,
                                      time.time() - p0)
        except Exception as e:  # noqa: BLE001
            results[i] = _part_record(
                n, len(parts), lane.id, lane.name,
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                time.time() - p0)
        taken[lane.id].append(n)

    async def run_lane(lane) -> None:
        if buckets is not None:
            for i in buckets[[a.id for a in lanes].index(lane.id)]:
                await do_part(lane, i)
            return
        while True:
            try:
                i = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await do_part(lane, i)

    # ---- 第一波：所有车道并行开跑，谁空谁领下一个 ----
    await asyncio.gather(*(run_lane(lane) for lane in lanes))

    # ---- 第二波：失败的部分顺序补跑一次 ----
    # 只补失败的那几个，而且是**串行**：这时候并行已经没意义了
    # （大概率是某家掉线），开着 fallback 让它自己去别的家找个能用的。
    retried = 0
    failed = [i for i, r in enumerate(results) if not r.get("ok")]
    if retry_failed and failed:
        for i in failed:
            n = i + 1
            prompt = _build_prompt(template, parts[i], n, len(parts))
            p0 = time.time()
            try:
                r = await pool.ask(prompt, timeout=timeout, reset=True,
                                   fallback=True)
                if r.get("ok"):
                    # 把这一部分从**原车道**的归属里摘掉再记到新车道。
                    # 不摘的话 assignment 里同一个编号会同时挂在两家名下
                    # （第一波记在 b 名下、补跑成功后记在 auto 名下），
                    # 看上去像"这部分被跑了两遍"，而且让 assignment
                    # 不再是 parts 的一个划分 —— 报告就不敢信了。
                    prev = results[i].get("provider")
                    if prev and prev in taken:
                        taken[prev] = [x for x in taken[prev] if x != n]
                    results[i] = _part_record(
                        n, len(parts), r.get("provider") or "auto",
                        r.get("provider_name") or "", r, time.time() - p0,
                        "(补跑)")
                    taken.setdefault(r.get("provider") or "auto", []).append(n)
                    retried += 1
            except Exception:  # noqa: BLE001
                pass

    wall = round(time.time() - t0, 2)
    ok_results = [r for r in results if r.get("ok")]
    # 串行耗时 = 各部分耗时之和（含各自开浏览器的固定开销），
    # 拿它跟墙钟比才是诚实的加速比。
    serial = round(sum(r.get("elapsed", 0) for r in ok_results), 2)

    return {
        "ok": len(ok_results) == len(results),
        "partial": bool(ok_results) and len(ok_results) < len(results),
        "parts_total": len(results),
        "parts_ok": len(ok_results),
        "wall_clock": wall,
        "sequential_estimate": serial,
        "speedup": round(serial / wall, 2) if wall > 0 and serial > 0 else None,
        "lanes": [a.id for a in lanes],
        "disabled_skipped": disabled_skipped,
        "schedule": schedule,
        "assignment": {k: sorted(v) for k, v in taken.items() if v},
        "retried": retried,
        "parts": results,
        "merged": _merge(results, merge, label),
        "hint": ("并行不共享网页端上文：每个部分都是全新会话。"
                 "需要带前文的拆法请改用 thread 串行追问。"),
    }
