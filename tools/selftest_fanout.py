"""离线回归测试：并行外包的车道分配、模板拼接、结果合并。

    python tools/selftest_fanout.py

不联网、不开浏览器、不消耗账号额度（车道选择那部分用假的 adapter 替身）。

为什么这些逻辑值得单独测
------------------------
并行最容易出的不是"跑不起来"，而是**结果静默错乱**：

1. 合并顺序 —— `asyncio.gather` 的完成顺序是乱的。如果按完成顺序拼，
   用户拿到的是一篇段落顺序被打乱的文章，而且看不出哪里不对。
   必须按**原始 parts 顺序**合并。
2. 调度策略 —— 默认是"动态抢夺"（谁先空谁领下一个），因为各站速度差 10 倍：
   DeepSeek 走纯 HTTP 一段 1~3 秒，元宝/豆包要开浏览器一段 20~28 秒。
   开跑前平均分（roundrobin）会让快车道干完就晾着，总时长由最慢车道决定。
   调度分错了不会报错、只是慢，属于最难发现的那类退化。
3. `reset=True` —— 不传的话同一家的多个部分会被网页端当成同一个话题的
   连续追问，第二个部分会把第一个的答案当历史带上（实测回填 2264 字节）。
   既不省钱又有串扰风险，而且**看起来完全正常**。
4. 模板占位符 —— 用户写了模板却忘了 {part}，如果直接丢内容，
   等于把要处理的正文静默扔掉、只把指令发出去。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import fanout  # noqa: E402
from core.fanout import _build_prompt, _merge, _part_record  # noqa: E402

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


def check_true(name: str, got) -> None:
    check(name, bool(got), True)


# ------------------------------------------------------------ 模板拼接
print("=== 模板拼接 ===")
check("有 {part} → 原样替换", _build_prompt("翻译：{part}", "正文A", 1, 3), "翻译：正文A")
check("{n} 和 {total} 都替换",
      _build_prompt("这是第{n}/{total}段：{part}", "正文A", 2, 5), "这是第2/5段：正文A")
check("★ 忘了写 {part} → 内容追加在末尾（不能静默丢弃）",
      _build_prompt("翻译下面这段", "正文A", 1, 3), "翻译下面这段\n\n正文A")
check("模板为空 → 就是内容本身", _build_prompt("", "正文A", 1, 3), "正文A")
check("同一占位符出现多次都替换",
      _build_prompt("{part} 和 {part}", "X", 1, 1), "X 和 X")


# ------------------------------------------------------------ 合并
print()
print("=== 合并 ===")


def _rec(n: int, total: int, answer: str, ok: bool = True, provider: str = "a") -> dict:
    """造一条"部分结果"夹具。

    ★ 必须走生产代码自己的构造函数 `_part_record`，不能手写 dict。
      这里踩过一次很典型的坑（2026-10-01）：夹具手写了 `"total": 3`，
      而生产端 `do_part` **从来没写过 `total`** —— 于是 `_merge` 里
      `label.format(n=..., total=...)` 每次都 KeyError、被 except 吞掉，
      调用方传的 `label` 在线上完全失效，而这条"自定义 label 生效"的断言
      一直是绿的。**两边形状不一致 = 测试在自说自话。**
      现在夹具就是生产函数的输出，形状再也漂不了。
    """
    return _part_record(n, total, provider, provider.upper(),
                        {"ok": ok, "answer": answer,
                         "error": "" if ok else "超时", "via": "fake"}, 0.01)


# 故意把完成顺序打乱：parts 列表按 1/2/3 给，但 dict 是按 2/3/1 顺序构造的
results = [_rec(1, 3, "答案一"), _rec(3, 3, "答案三"), _rec(2, 3, "答案二")]
check_true("★ 结果记录自带 label 要用的 total（形状契约）",
           all("total" in r for r in results))
sec = _merge(results, "sections", "第{n}部分")
check_true("sections 合并带小标题", "## 第1部分" in sec and "## 第3部分" in sec)
check_true("★ 按传入顺序合并（不是完成顺序）",
           sec.index("答案一") < sec.index("答案二") < sec.index("答案三"))
check("concat 合并 = 纯拼接",
      _merge(results, "concat", ""), "答案一\n\n答案二\n\n答案三")
check("json 合并 = 不合并（只留 parts）", _merge(results, "json", ""), "")

mixed = [_rec(1, 3, "答一"), _rec(2, 3, "", ok=False), _rec(3, 3, "答三")]
m2 = _merge(mixed, "sections", "第{n}部分")
check_true("失败的部分不进合并稿", "答一" in m2 and "答三" in m2 and "答二" not in m2)
check_true("自定义 label 生效",
           "## 片段1" in _merge(results, "sections", "片段{n}"))
check_true("★ label 里的 {total} 也能用",
           "## 片段1/3" in _merge(results, "sections", "片段{n}/{total}"))
check_true("label 占位符写坏也不崩",
           "## " in _merge(results, "sections", "片段{n}{oops}"))


# ------------------------------------------------------------ 车道分配
print()
print("=== 车道分配（用假 adapter / 假 ask，不碰浏览器）===")


class FakeAdapter:
    def __init__(self, pid: str):
        self.id = pid
        self.name = pid.upper()
        self.tags = ["general"]
        self.http_capable = False

    def enabled(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"<{self.id}>"


ALL_LANES = [FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c")]


def run_fanout(parts, lanes, *, providers=None, schedule="dynamic",
               speed=None, max_lanes=None, fail_lanes=(), label=None):
    """用假 ask 跑一遍 fanout，返回 (结果, 调用记录)。

    `speed` 是 {车道id: 每部分秒数}，用来造出"快慢车道"的真实差距 ——
    没有速度差就测不出动态抢夺和轮转的区别。
    `fail_lanes` 里的车道在**第一波**（fallback=False）会失败，第二波补跑
    才成功 —— 用来测降级路径。默认空集：失败车道必须显式指定。
    第一版把所有用例都套上了"车道 b 必失败"，于是主用例里 2/5 两段
    被补跑、assignment 出现重复编号，看着像调度写错了，其实是我自己
    的假替身在到处放火。

    ★ 假 ask 的签名必须跟真的 `pool.ask` **逐字对齐**（provider / thread /
    reset / timeout / fallback / no_mode / grab_images）。第一版漏了 `reset`，
    结果 fanout 传进来的 `reset=True` 直接抛 TypeError，被 do_part 里的
    `except Exception` 变成"这个部分失败了" —— 测试不但没测到调度，
    还差点让我以为调度写错了。假替身的接口漂移是这类测试的头号假信号。
    """
    calls: list[dict] = []
    speed = speed or {}

    async def fake_ask(prompt, *, provider=None, thread=None, reset=None,
                       timeout=None, fallback=True, no_mode=False,
                       grab_images=False):
        calls.append({"provider": provider, "prompt": prompt, "reset": reset,
                      "fallback": fallback, "thread": thread})
        await asyncio.sleep(speed.get(provider, 0.02))
        if provider in fail_lanes and not fallback:
            return {"ok": False, "provider": provider, "answer": "",
                    "error": "模拟未登录", "via": "", "elapsed": 0.0}
        return {"ok": True, "provider": provider, "provider_name": provider,
                "answer": f"[{provider}]{prompt}", "error": "", "via": "fake",
                "elapsed": speed.get(provider, 0.02)}

    real_ask, real_pick = fanout.pool.ask, fanout._pick_lanes
    fanout.pool.ask = fake_ask

    async def _pick(providers_, prompt_, max_lanes_):
        # 替身也要尊重 providers 参数，否则"指定车道"这条根本没被测到
        picked = ([l for l in lanes if l.id in providers_]
                  if providers_ else list(lanes))
        # 真 `_pick_lanes` 现在返回 (车道, 被跳过的站点)
        return picked[:max_lanes_], []

    fanout._pick_lanes = _pick
    kw = {"label": label} if label is not None else {}
    try:
        res = asyncio.run(fanout.fanout(
            parts, providers=providers,
            max_lanes=max_lanes or len(lanes), schedule=schedule, **kw))
    finally:
        fanout.pool.ask = real_ask
        fanout._pick_lanes = real_pick
    return res, calls


def flat(assignment) -> list[int]:
    """把 assignment 摊平成一个编号列表，用来验证"不重不漏"。"""
    out: list[int] = []
    for v in assignment.values():
        out.extend(v)
    return sorted(out)


PARTS7 = [f"P{i}" for i in range(1, 8)]

# --- 轮转：开跑前平均分好，用于"刻意均摊到各家、别把某一家额度打光" ---
r, calls = run_fanout(PARTS7, ALL_LANES, schedule="roundrobin")
check("7 部分 3 车道 → 不超车道数", len(r["lanes"]), 3)
check("轮转：车道 a 拿 1/4/7", r["assignment"]["a"], [1, 4, 7])
check("轮转：车道 b 拿 2/5", r["assignment"]["b"], [2, 5])
check("轮转：车道 c 拿 3/6", r["assignment"]["c"], [3, 6])
check("轮转：7 个部分不重不漏", flat(r["assignment"]), list(range(1, 8)))
check("每个部分恰好被跑一次", sorted(c["prompt"] for c in calls), sorted(PARTS7))
check("合并稿顺序 = 输入顺序",
      [r["parts"][i]["n"] for i in range(7)], [1, 2, 3, 4, 5, 6, 7])
check_true("合并稿里 P1 在 P7 之前", r["merged"].index("P1") < r["merged"].index("P7"))
check("全部成功 → ok=True", r["ok"], True)
check("全部成功 → partial=False", r["partial"], False)
check_true("报告了加速比字段", r.get("speedup") is not None)
check("返回值里如实报告 schedule", r.get("schedule"), "roundrobin")
check("★ 每个部分都强制 reset=True（否则同家多个部分会串成连续追问）",
      sorted({c["reset"] for c in calls}), [True])
check("★ 第一波不开 fallback（失败要留在本车道，才看得出是哪家的问题）",
      sorted({c["fallback"] for c in calls}), [False])
# ★ 端到端把 label 走一遍：夹具可以伪造形状，这里是从真 fanout() 的返回值里
#   读 merged —— 上面那条"夹具自造 total"的假绿就是靠这条堵住的。
r_lbl, _ = run_fanout(["X", "Y"], ALL_LANES, label="片段{n}/{total}")
check_true("★ 端到端：自定义 label（含 {total}）真的出现在 merged 里",
           "## 片段1/2" in r_lbl["merged"] and "## 片段2/2" in r_lbl["merged"])
check_true("★ 端到端：每个部分的结果里都带 total",
           all(p.get("total") == 2 for p in r_lbl["parts"]))

# --- 动态抢夺（默认）：谁先空谁领下一个，快的车道自然多领 ---
# a 快（10ms）/ b 中（50ms）/ c 慢（150ms），7 个部分。
#   轮转：a 拿 3 个、b 拿 2 个、c 拿 2 个 → 墙钟 ≈ c 的 2 段 = 300ms
#   动态：a 干完一个马上领下一个，最终墙钟 ≈ c 的 1 段 = 150ms
SPEED = {"a": 0.01, "b": 0.05, "c": 0.15}
rr_d, _ = run_fanout(PARTS7, ALL_LANES, schedule="roundrobin", speed=SPEED)
dy_d, cd = run_fanout(PARTS7, ALL_LANES, schedule="dynamic", speed=SPEED)
check("动态：默认车道数不变", len(dy_d["lanes"]), 3)
check("动态：7 个部分不重不漏", flat(dy_d["assignment"]), list(range(1, 8)))
check("动态：每个部分恰好被跑一次", len(cd), 7)
check("动态：返回值里如实报告 schedule", dy_d.get("schedule"), "dynamic")
check_true("★ 动态：最快的车道分到的不比最慢的少",
           len(dy_d["assignment"].get("a", [])) >= len(dy_d["assignment"].get("c", [])))
check_true("★ 同样的速度分布下，动态抢夺的墙钟明显短于轮转",
           dy_d["wall_clock"] < rr_d["wall_clock"] * 0.8)
check_true("动态的加速比高于轮转",
           (dy_d["speedup"] or 0) > (rr_d["speedup"] or 0))
# 动态下 c 只该领到 1 个（它慢，抢不过别人）—— 这正是不做预分配的意义
check("★ 动态：最慢的车道只领到 1 个（不做预分配的意义所在）",
      len(dy_d["assignment"].get("c", [])), 1)

# 车道比部分多 → 车道数收敛到部分数（别开空窗口）
r2, _ = run_fanout(["X", "Y"], ALL_LANES)
check("2 部分 3 车道 → 只用 2 条车道", len(r2["lanes"]), 2)

# 显式指定车道
r3, _ = run_fanout(["X", "Y", "Z"], ALL_LANES, providers=["c"])
check("指定单车道 → 只能 1 条", r3["lanes"], ["c"])
check("指定单车道 → 3 个部分都归它", flat(r3["assignment"]), [1, 2, 3])

# 空输入
r4 = asyncio.run(fanout.fanout([]))
check("空 parts → 明确报错", r4["ok"], False)
check_true("空 parts 的报错文案有意义", "parts" in r4.get("error", ""))

# 未知站点名 → 明确报错而不是静默忽略
r5 = asyncio.run(fanout.fanout(["X"], providers=["不存在的站点"]))
check("未知 provider → 失败", r5["ok"], False)
check_true("未知 provider 的报错里点出名字", "不存在的站点" in r5.get("error", ""))

# ------------------------------------------------------------ enabled 过滤
print()
print("=== 显式点名也要尊重 enabled（config 里关掉的不能被拉起来）===")


class OffAdapter(FakeAdapter):
    def enabled(self) -> bool:
        return False


_REAL_ALL = fanout.provider_pkg.all_adapters
fanout.provider_pkg.all_adapters = lambda: [FakeAdapter("a"), OffAdapter("b")]
try:
    _lanes, _skipped = asyncio.run(fanout._pick_lanes(["a", "b"], "x", 4))
finally:
    fanout.provider_pkg.all_adapters = _REAL_ALL
check("启用的站点被选中", [l.id for l in _lanes], ["a"])
check("★ 关掉的站点被跳过并如实回报（以前会照样被拉起来）", _skipped, ["b"])
# 全被关掉 → 明确失败，且报错里点名是哪几家、为什么
fanout.provider_pkg.all_adapters = lambda: [OffAdapter("b")]
try:
    r_off = asyncio.run(fanout.fanout(["X"], providers=["b"]))
finally:
    fanout.provider_pkg.all_adapters = _REAL_ALL
check("全被关掉 → 明确失败", r_off["ok"], False)
check_true("★ 报错里点名是 enabled: false 跳过的",
           "enabled: false" in r_off.get("error", "") and "b" in r_off.get("error", ""))

# 未知调度名 → 明确报错（不能静默退回 dynamic，否则"刻意均摊"的意图落空）
# ★ 这两条也必须走 run_fanout 的桩：第一版直接调 fanout.fanout()，
#   结果真的拉起了一个元宝浏览器、把 "X" 发出去了 —— 一个号称
#   "不联网不开浏览器"的自检脚本，最不该犯的就是这个。
r5b, _ = run_fanout(["X"], ALL_LANES, schedule="RoundRobin ")
check("schedule 大小写/空格会被归一化", r5b.get("ok"), True)
check("归一化后如实报告成小写", r5b.get("schedule"), "roundrobin")
r5c = asyncio.run(fanout.fanout(["X"], schedule="round_robin"))
check("未知 schedule → 失败", r5c["ok"], False)
check_true("未知 schedule 的报错里列出可用取值",
           "dynamic" in r5c.get("error", "") and "roundrobin" in r5c.get("error", ""))

# ------------------------------------------------------------ 部分失败
print()
print("=== 部分失败的降级 ===")

# 车道 a 拿 1/3，车道 b 拿 2；b 在第一波失败（fallback=False），
# 第二波补跑时 fallback=True → 成功
r6, c6 = run_fanout(["A", "B", "C"], [FakeAdapter("a"), FakeAdapter("b")],
                    fail_lanes={"b"})
check("失败部分被补跑 → 最终全成功", r6["ok"], True)
check("补跑次数被记录", r6["retried"], 1)
check_true("补跑的 via 有标记", "补跑" in r6["parts"][1]["via"])
check("★ 补跑那一波才开 fallback（让它去别的家找能用的）",
      any(c["fallback"] is True for c in c6), True)
check("补跑也强制 reset=True",
      all(c["reset"] is True for c in c6), True)
# 补跑成功后，这个部分必须从原车道名下摘掉，否则同一个编号挂在两家名下
check("★ 补跑后 assignment 仍是 parts 的一个划分（不重不漏）",
      flat(r6["assignment"]), [1, 2, 3])
check("★ 补跑的部分只挂在最终那家名下",
      [k for k, v in r6["assignment"].items() if 2 in v], ["auto"])

print()
if fails:
    print(f"✗ {len(fails)}/{total} 项失败：{'、'.join(fails)}")
    raise SystemExit(1)
print(f"✓ 全部通过（{total} 项）")
