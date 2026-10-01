"""元宝"回答被腰斩"时序采样器。

用法（网关已在运行）：
    python tools/probe_yuanbao_live.py "提问内容" [thread]

做法：后台线程发 /api/ask，主线程每 ~1.2s 打一次 /api/debug/dom?kind=watch，
把"答案候选的文字长度随时间怎么变"和"当时页面上有没有停止按钮"对齐到一条时间轴上。

为什么需要它：定位"答案只捞回一行表头"这类问题时，光看最终结果看不出
是被截断还是压根没生成完 —— 必须看到"抓取那一刻文本有多长、之后又长到多长"。
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"
PROVIDER = "yuanbao"


def post(path: str, payload: dict, timeout: float = 600.0):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def watch():
    return post("/api/debug/dom", {"provider": PROVIDER, "kind": "watch"}, timeout=30)


def summarize(w: dict) -> str:
    """把一帧压成一行：最重要的三类候选长度 + 停止按钮。"""
    by_sel: dict[str, list] = {}
    for c in w.get("cands", []):
        by_sel.setdefault(c["sel"], []).append(c)
    parts = []
    for sel, items in by_sel.items():
        tops = [c for c in items if not c["nested"] and c["visible"]]
        if tops:
            lens = ",".join(str(c["len"]) for c in tops)
            parts.append(f"{sel[:26]}顶层[{lens}]")
    stop = "停" if w.get("stop_buttons") else "-"
    cot = w.get("flags", {}).get(".hyc-component-deepsearch-cot", -1)
    return f"{stop} cot={cot} | " + " ".join(parts)


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else (
        "用表格对比番茄工作法和时间盒（Timeboxing）的区别，至少列出6个维度。")
    thread = sys.argv[2] if len(sys.argv) > 2 else "yb-live"

    box: dict = {}

    def ask() -> None:
        try:
            box["r"] = post("/api/ask", {"prompt": prompt, "provider": PROVIDER,
                                         "thread": thread, "reset": True})
        except urllib.error.HTTPError as e:
            box["r"] = {"ok": False, "error": e.read().decode("utf-8", "replace")}
        except Exception as e:  # noqa: BLE001
            box["r"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    t = threading.Thread(target=ask, daemon=True)
    t0 = time.time()
    t.start()
    print(f"提问：{prompt}\n")

    while t.is_alive():
        try:
            line = summarize(watch())
        except Exception as e:  # noqa: BLE001
            line = f"<watch 失败 {type(e).__name__}>"
        print(f"[{time.time() - t0:6.1f}s] {line}", flush=True)
        time.sleep(1.2)

    t.join(timeout=5)
    print("\n===== 最终结果 =====")
    r = box.get("r") or {}
    ans = r.get("answer") or ""
    print(json.dumps({k: v for k, v in r.items() if k != "answer"},
                     ensure_ascii=False, indent=2))
    print(f"answer 字数 = {len(ans)}")
    print("---- answer 全文 ----")
    print(ans)


if __name__ == "__main__":
    main()
