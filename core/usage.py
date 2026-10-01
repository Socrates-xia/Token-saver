"""用量与省钱统计。

网页端不会返回真实 token 数，这里用字符级估算：
  CJK 约 0.7 token/字，其余约 0.25 token/字符。
配合 settings.pricing 里的"参考单价"，估算"这笔调用若走主模型原本要花多少"。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime
from typing import Any

from . import settings

_LOCK = threading.Lock()
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk * 0.7 + other * 0.25))


def _blank() -> dict:
    return {
        "calls": 0,
        "ok": 0,
        "fail": 0,
        "prompt_tokens": 0,
        "answer_tokens": 0,
        "elapsed": 0.0,
        "saved_usd": 0.0,
        "by_provider": {},
        "history": [],
    }


def load() -> dict:
    if not settings.USAGE_FILE.exists():
        return _blank()
    try:
        data = json.loads(settings.USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _blank()
    base = _blank()
    base.update(data)
    return base


def save(data: dict) -> None:
    settings.USAGE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------- 净省算法
#
# 你自己一问一答直接做完要花：  prompt × 输入价 + answer × 输出价
# 外包给网页端之后你要花：      prompt × 输入价 + answer × 输入价(读作 context)
#                              + 工具调用的固定往返开销
#
# 差额只有 answer 那一段的"输出价 - 输入价"，
# 而工具往返是固定成本 —— 所以**答案太短时外包其实是亏的**。
TOOL_INPUT_TOKENS = 180     # 工具定义/请求封装/网关说明这类被打进上下文的部分
TOOL_OUTPUT_TOKENS = 120    # 你这侧实际敲出去的那点工具调用文本


def overhead_tokens() -> int:
    return TOOL_INPUT_TOKENS + TOOL_OUTPUT_TOKENS


def overhead_usd() -> float:
    p = settings.get()["pricing"]
    return (TOOL_INPUT_TOKENS * p["reference_input_usd_per_mtok"]
            + TOOL_OUTPUT_TOKENS * p["reference_output_usd_per_mtok"]) / 1_000_000.0


def net_saved_of(answer_tokens: int) -> float:
    """这笔调用真正净省多少（已扣掉工具往返开销）。"""
    p = settings.get()["pricing"]
    gross = answer_tokens * (p["reference_output_usd_per_mtok"]
                             - p["reference_input_usd_per_mtok"]) / 1_000_000.0
    return gross - overhead_usd()


def break_even_tokens() -> int:
    """答案至少多少 token 才不亏本。低于这个值建议直接自己回答。"""
    p = settings.get()["pricing"]
    spread = p["reference_output_usd_per_mtok"] - p["reference_input_usd_per_mtok"]
    if spread <= 0:
        return 10 ** 9
    return int(overhead_usd() * 1_000_000.0 / spread)


def cost_of(prompt_tokens: int, answer_tokens: int) -> float:
    p = settings.get()["pricing"]
    return (prompt_tokens * p["reference_input_usd_per_mtok"]
            + answer_tokens * p["reference_output_usd_per_mtok"]) / 1_000_000.0


def record(pid: str, prompt: str, answer: str, elapsed: float, ok: bool) -> dict:
    pt = estimate_tokens(prompt)
    at = estimate_tokens(answer)
    saved = cost_of(pt, at)

    with _LOCK:
        data = load()
        data["calls"] += 1
        data["ok" if ok else "fail"] += 1
        if ok:
            data["prompt_tokens"] += pt
            data["answer_tokens"] += at
            data["elapsed"] += elapsed
            data["saved_usd"] += saved

        bp = data["by_provider"].setdefault(
            pid, {"calls": 0, "ok": 0, "fail": 0, "elapsed": 0.0,
                  "answer_tokens": 0, "saved_usd": 0.0}
        )
        bp["calls"] += 1
        bp["ok" if ok else "fail"] += 1
        if ok:
            bp["answer_tokens"] += at
            bp["elapsed"] += elapsed
            bp["saved_usd"] += saved

        data["history"].append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "provider": pid,
            "ok": ok,
            "elapsed": round(elapsed, 2),
            "prompt_tokens": pt,
            "answer_tokens": at,
            "saved_usd": round(saved, 6),
        })
        data["history"] = data["history"][-300:]
        save(data)
    return {"prompt_tokens": pt, "answer_tokens": at, "saved_usd": saved}


def summary() -> dict[str, Any]:
    data = load()
    calls = data["calls"]
    ans_tokens = data["answer_tokens"]
    net = net_saved_of(ans_tokens) if data["ok"] else 0.0
    return {
        "calls": calls,
        "ok": data["ok"],
        "fail": data["fail"],
        "success_rate": round(data["ok"] / calls * 100, 1) if calls else 0.0,
        "avg_elapsed": round(data["elapsed"] / data["ok"], 2) if data["ok"] else 0.0,
        "avg_answer_tokens": round(ans_tokens / data["ok"], 1) if data["ok"] else 0.0,
        "tokens_saved": data["prompt_tokens"] + ans_tokens,
        "usd_saved": round(data["saved_usd"], 4),
        "cny_saved": round(data["saved_usd"] * 7.1, 2),
        # 扣除工具往返开销之后真正落进口袋的部分
        "usd_net_saved": round(net, 4),
        "cny_net_saved": round(net * 7.1, 2),
        "overhead_usd": round(overhead_usd(), 6),
        "break_even_tokens": break_even_tokens(),
        "by_provider": data["by_provider"],
        "recent": data["history"][-20:][::-1],
        "reference_model": settings.get()["pricing"]["reference_model"],
    }


def reset() -> None:
    with _LOCK:
        save(_blank())


# ---------------------------------------------------------------- 异步外壳
#
# 下面这组 `a*` 是给**事件循环里**的调用方用的（`core/pool.py`、`server.py`）。
#
# 为什么需要：`record()` / `load()` 每次都要把整份 `usage.json` 读进来、
# 改完再整份写回去。文件本身只有几十 KB，单看一次 I/O 微不足道，但它是
# **同步**的 —— 一旦在协程里直接调用，整个事件循环都会被按住：
# 那一刻正在并行的其它外包车道、控制台的 15 秒轮询、SSE 推送全都得排队
# 等这次磁盘写完。fan-out 一开就是好几路并发，正是最容易撞上的时候。
#
# 丢进线程池的代价接近 0，换来的是 I/O 期间事件循环照常转。
# 工具脚本（`tools/*.py`）不在事件循环里，继续用上面的同步版本，不必改。
async def arecord(pid: str, prompt: str, answer: str,
                  elapsed: float, ok: bool) -> dict:
    return await asyncio.to_thread(record, pid, prompt, answer, elapsed, ok)


async def aload() -> dict:
    return await asyncio.to_thread(load)


async def asummary() -> dict[str, Any]:
    return await asyncio.to_thread(summary)


async def areset() -> None:
    await asyncio.to_thread(reset)
