"""排障：把 DeepSeek 真实 SSE 流原样 dump 出来，看各字段到底长什么样。

用途是定位"思考过程混进答案"这类解析问题 —— 只看文档猜字段名会一直猜错，
必须看真流。

    python tools/probe_sse.py "随便问一句短的"
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import http_deepseek as H  # noqa: E402
from core import settings  # noqa: E402


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "1+1等于几？只回答数字。"

    cred = H.load_credentials()
    if not cred.get("cookie"):
        raise SystemExit("没有凭证，先跑 tools/test_http.py --harvest")

    sid = H.create_session(cred)
    print(f"会话 {sid}\n")

    path = "/api/v0/chat/completion"
    pow_header = H.fetch_pow(path, cred)
    body = {
        "chat_session_id": sid,
        "parent_message_id": None,
        "prompt": prompt,
        "ref_file_ids": [],
        "thinking_enabled": True,
        "search_enabled": True,
        "preempt": False,
    }
    req = urllib.request.Request(
        H.BASE + path, method="POST",
        data=json.dumps(body).encode(),
        headers=H._headers(cred, with_pow=pow_header))

    # 记录：字段路径 -> 被解析成什么
    paths: dict[str, list[str]] = {}
    frag_events: list[str] = []
    # 片段元数据事件的原样 JSON（最多留 6 条，够看清结构了）
    frag_raw: list[str] = []
    last_frag_sig = ""
    raw_all: list[str] = []
    stream = H._Stream()
    n = 0
    with urllib.request.urlopen(req, timeout=180) as resp:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                d = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(d, dict):
                continue
            n += 1
            raw_all.append(f"[{n}] {json.dumps(d, ensure_ascii=False)}")
            stream.feed(line)
            p = str(d.get("p") or d.get("type") or "(无p)")
            v = d.get("v")
            kind = type(v).__name__
            snippet = (v if isinstance(v, str) else json.dumps(
                v, ensure_ascii=False))[:40]
            paths.setdefault(p, [])
            if len(paths[p]) < 3:
                paths[p].append(f"[{kind}] {snippet!r}")

            # ★ 片段列表可能藏在任意一层（实测在 v.response.fragments，
            #   而且这条事件**没有 p 字段**），所以必须递归找，不能只看 p。
            found: list[list] = []

            def walk(o, _acc=found):
                if isinstance(o, dict):
                    fr = o.get("fragments")
                    if isinstance(fr, list) and fr:
                        _acc.append(fr)
                    for vv in o.values():
                        walk(vv)
                elif isinstance(o, list):
                    for vv in o:
                        walk(vv)

            walk(d)
            for fr in found:
                desc = ", ".join(
                    f"#{f.get('id')}:{f.get('type')}({len(str(f.get('content') or ''))}字)"
                    for f in fr if isinstance(f, dict))
                sig = desc
                if sig and sig != last_frag_sig:
                    last_frag_sig = sig
                    frag_events.append(f"[事件#{n}] {desc}")
                    if len(frag_raw) < 6:
                        frag_raw.append(json.dumps(d, ensure_ascii=False)[:600])

    print(f"共 {n} 个数据事件。按字段路径归类：\n")
    for p, samples in sorted(paths.items()):
        print(f"  p = {p!r}")
        for s in samples:
            print(f"        {s}")
    print()
    print("片段列表演化（type 决定内容归属）：")
    for f in frag_events[:12]:
        print(f"    {f}")
    print()
    print("片段元数据事件原样（前几条）：")
    for r in frag_raw:
        print(f"    {r}")
    print()
    # 原流落盘：字段名/嵌套形态靠猜永远猜不对，留一份真流最省事
    try:
        out = settings.DATA_DIR / "sse_raw.txt"
        out.write_text("\n".join(raw_all), encoding="utf-8")
        print(f"完整原始流已写入 {out}（{len(raw_all)} 条）")
    except Exception as e:  # noqa: BLE001
        print(f"（原始流写盘失败：{e}）")
    print()
    print("前 25 条原样：")
    for r in raw_all[:25]:
        print(f"    {r[:220]}")
    print()
    print("当前解析器判定的结果：")
    print(f"    answer   ({len(stream.answer)} 字): {stream.answer[:120]!r}")
    print(f"    thinking ({len(stream.thinking)} 字): {stream.thinking[:120]!r}")
    print(f"    message_id: {stream.message_id}")


if __name__ == "__main__":
    main()
