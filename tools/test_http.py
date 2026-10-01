"""验证 DeepSeek 纯 HTTP 直连是否可用。

三种用法：
    # 只看状态（不需要浏览器，不联网）
    python tools/test_http.py --status

    # 开一次浏览器把凭证收下来（Bearer 只在 localStorage 里，必须这么做）
    python tools/test_http.py --harvest

    # 真的问一次，打印答案和耗时
    python tools/test_http.py --ask "用一句话解释什么是哈希"

凭证收下来之后，日常调用就完全不需要浏览器了 —— 服务器那边
（pool）会自动优先走这条路。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import http_deepseek  # noqa: E402
from core import settings  # noqa: E402


def show_status() -> None:
    st = http_deepseek.status()
    print("=== 纯 HTTP 直连状态 ===")
    print(f"  全局开关 http_first : {st.get('enabled_globally')}")
    print(f"  凭证 cookie         : {'有' if st['credentials'] else '无'}")
    print(f"  凭证 bearer         : {'有' if st['has_bearer'] else '无'}")
    saved = st.get("saved_at")
    if saved:
        import datetime
        when = datetime.datetime.fromtimestamp(saved).strftime("%m-%d %H:%M:%S")
        print(f"  凭证收取时间        : {when}（{int(time.time() - saved)} 秒前）")
    print(f"  PoW 求解器          : {'就绪' if st['pow_ready'] else '不可用'}")
    if not st["pow_ready"]:
        print(f"     原因：{st['pow_reason']}")
    print(f"  已记录直连会话      : {st['sessions']} 个")
    print()
    if not st["credentials"] or not st["has_bearer"]:
        print("→ 凭证不全，先跑 --harvest（会开一次浏览器）")
    elif not st["pow_ready"]:
        print("→ PoW 不可用，直连走不通；装 wasmtime 后重试："
              "pip install wasmtime")
    else:
        print("→ 直连条件齐备，可以直接 --ask 试一下")


async def harvest() -> None:
    """开一次浏览器，把 Cookie + Bearer 收下来。"""
    from core import adapters as pkg
    from core import browser

    a = pkg.get("deepseek")
    if not a:
        raise SystemExit("找不到 deepseek 适配器")
    print("正在打开浏览器（需要已登录 DeepSeek）…")
    page = await browser.manager.ensure_page(a, headless=False)
    await page.wait_for_timeout(1500)
    if not await a.is_logged_in(page):
        print("⚠️  看起来还没登录 DeepSeek。请在弹出的窗口里登录后重跑本命令。")
        await browser.manager.close_all()
        raise SystemExit(2)
    await http_deepseek.harvest(page)
    print("凭证已收下。之后就可以纯 HTTP 了（本命令可以关掉浏览器了）。")
    await browser.manager.close_all()


async def ask(prompt: str, thread: str, reset: bool) -> None:
    from core import pow_deepseek
    pow_ok, pow_why = pow_deepseek.available()
    if not pow_ok:
        raise SystemExit(f"PoW 不可用：{pow_why}")

    print(f"提问：{prompt}")
    print("（纯 HTTP，不开浏览器）…")
    t0 = time.time()
    try:
        out = await http_deepseek.ask(prompt, thread=thread, reset=reset,
                                      thinking=True, timeout=180.0)
    except http_deepseek.HttpAuthError as e:
        print(f"✗ 凭证不可用：{e}")
        print("  → 跑一次 --harvest 重新收凭证")
        raise SystemExit(1)
    except Exception as e:  # noqa: BLE001
        print(f"✗ 失败：{type(e).__name__}: {e}")
        raise SystemExit(1)

    dt = time.time() - t0
    ans = out.get("answer") or ""
    think = out.get("thinking") or ""
    print(f"\n✓ 成功（{dt:.2f}s）"
          f"{'，接上了上文' if out.get('resumed') else '，新会话'}")
    print(f"  会话 id: {out.get('session_id')}")
    print(f"  思考过程: {len(think)} 字"
          f"{'（前 80 字：' + think[:80].replace(chr(10), ' ') + '）' if think else ''}")
    print(f"  正文: {len(ans)} 字")
    print("\n--- 回答 ---")
    print(ans)
    print("--- 完 ---")
    print(f"\n对比：浏览器路线同一问通常要 7~15s 才出第一个字。"
          f"直连总耗时 {dt:.2f}s。")


def selftest() -> None:
    """离线回归测试：不联网、不开浏览器。

    重点盯**最容易悄悄坏掉**的部分 —— SSE 分片解析。它一旦判错，
    现象是"思考过程混进答案"或"答案重复拼接"，而且不会报错，
    只会安静地给出错误结果。所以这些用例要固化下来。
    """
    import json
    from core.http_deepseek import _Stream
    from core import http_deepseek as H
    from core import adapters as pkg

    fails: list[str] = []

    def check(name: str, got, want) -> None:
        ok = got == want
        print(f"  {'✓' if ok else '✗'} {name}")
        if not ok:
            print(f"      期望 {want!r}")
            print(f"      实际 {got!r}")
            fails.append(name)

    def stream(lines: list[str]) -> _Stream:
        s = _Stream()
        for l in lines:
            s.feed(l)
        return s

    def d(obj) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False)

    print("=== SSE 解析 ===")
    s = stream([
        d({"p": "response/thinking_content", "v": "思考甲"}),
        d({"p": "response/thinking_content", "v": "思考乙"}),
        d({"response_message_id": 42}),
        d({"p": "response/content", "v": "正文一"}),
        d({"p": "response/content", "v": "正文二"}),
    ])
    # ★ 关键：thinking_content 里同时含 "thinking" 和 "content"，
    #   判定顺序写错就会把整段思考混进答案。
    check("思考与正文分离", (s.thinking, s.answer),
          ("思考甲思考乙", "正文一正文二"))
    check("捕获 response_message_id", s.message_id, 42)

    check("reasoning 命名也认",
          [stream([d({"p": "response/reasoning", "v": "想"}),
                   d({"p": "response/content", "v": "答"})]).thinking,
           stream([d({"p": "response/reasoning", "v": "想"}),
                   d({"p": "response/content", "v": "答"})]).answer],
          ["想", "答"])

    # 累计式快照：不能拼成三遍
    check("累计式快照自适应",
          stream([d({"p": "response/content", "v": v})
                  for v in ("哈", "哈希", "哈希是")]).answer, "哈希是")

    # 数组片段形态
    sa = stream([d({"v": [{"type": "THINKING", "content": "推理"},
                          {"content": "甲"}]}),
                 d({"v": [{"content": "乙"}]})])
    check("数组片段形态", (sa.thinking, sa.answer), ("推理", "甲乙"))

    # 搜索/状态行不该污染正文
    check("搜索行不污染正文",
          stream([d({"p": "response/search_results", "v": {"query": "x"}}),
                  d({"p": "response/content", "v": "正文"})]).answer, "正文")

    # 脏数据不崩
    check("脏数据不崩",
          stream(["", "data: ", "data: [DONE]", "data: {坏",
                  "event: x", "data: [1,2,3]", "garbage"]).answer, "")

    print()
    print("=== 真实片段协议（2026-10-01 抓包实况）===")
    # 协议实况：带片段列表的元数据事件 + 落在 response/fragments/-1/content 的增量。
    # ★ 两个坑：
    #   (a) 类型字面量是 THINK，不是 THINKING/REASONING —— 漏认就把思考混进正文；
    #   (b) -1 对思考和正文是**同一个值**，只能靠片段类型判归属。
    s1 = stream([
        d({"v": {"response": {"message_id": 2, "fragments": [
            {"id": 2, "type": "THINK", "content": "我们需要"}]}}}),
        d({"p": "response/fragments/-1/content", "o": "APPEND", "v": "先想"}),
        d({"p": "response/status", "v": "FINISHED"}),
    ])
    check("THINK 片段进思考不进正文", (s1.thinking, s1.answer),
          ("我们需要先想", ""))

    # 思考结束后服务端新增一个 RESPONSE 片段，-1 随之指向新片段
    s2 = stream([
        d({"v": {"response": {"fragments": [
            {"id": 2, "type": "THINK", "content": "想"}]}}}),
        d({"p": "response/fragments/-1/content", "o": "APPEND", "v": "完了"}),
        d({"v": {"response": {"fragments": [
            {"id": 2, "type": "THINK", "content": "想完了"},
            {"id": 3, "type": "RESPONSE", "content": "答案"}]}}}),
        d({"p": "response/fragments/-1/content", "o": "APPEND", "v": "是 42"}),
    ])
    check("片段切换后 -1 改判为正文", (s2.thinking, s2.answer),
          ("想完了", "答案是 42"))

    # 同一片段（内容已增长）被重发，不能重复累加
    s3 = stream([
        d({"v": {"response": {"fragments": [
            {"id": 2, "type": "THINK", "content": "考虑"}]}}}),
        d({"v": {"response": {"fragments": [
            {"id": 2, "type": "THINK", "content": "考虑再考虑"}]}}}),
    ])
    check("片段重发不重复累加", s3.thinking, "考虑")

    # ★★ 最隐蔽的一条：真实流里绝大多数增量是**裸的 {"v": "…"}**，
    #    没有 p 字段，靠"承接上一条显式路径"决定归属。
    #    漏了这条 → 整段思考被当正文倒进 answer（不报错、只出错）。
    s4 = stream([
        d({"v": {"response": {"fragments": [
            {"id": 2, "type": "THINK", "content": "想"}]}}}),
        d({"p": "response/fragments/-1/content", "o": "APPEND", "v": "一"}),
        d({"v": "二"}),                                   # 裸增量 → 仍属 THINK
        d({"v": "三"}),
        d({"p": "response/fragments/-1/elapsed_secs", "o": "SET", "v": 0.41}),
        d({"p": "response/fragments", "o": "APPEND", "v": [
            {"id": 3, "type": "RESPONSE", "content": "答"}]}),
        d({"p": "response/fragments/-1/content", "v": "案"}),   # 无 o，也是增量
        d({"v": "！"}),                                   # 裸增量 → 属 RESPONSE
        d({"p": "response/status", "o": "SET", "v": "FINISHED"}),
    ])
    check("裸增量承接上一条路径", (s4.thinking, s4.answer), ("想一二三", "答案！"))

    # 非内容路径（status / elapsed_secs）不能顶掉内容落点
    check("状态行不顶掉内容落点",
          stream([d({"p": "response/fragments/-1/content", "o": "APPEND",
                     "v": "甲"}),
                  d({"p": "response/status", "v": "WIP"}),
                  d({"v": "乙"})]).answer, "甲乙")

    # 批处理形态：子事件要过同一个状态机，但不该污染正文
    s5 = stream([
        d({"p": "response", "o": "BATCH", "v": [
            {"p": "accumulated_token_usage", "v": 96},
            {"p": "quasi_status", "v": "FINISHED"}]}),
    ])
    check("BATCH 子事件不污染正文", (s5.thinking, s5.answer), ("", ""))

    check("BATCH 里的内容照常收下",
          stream([d({"p": "response", "o": "BATCH", "v": [
              {"p": "response/fragments/-1/content", "v": "内容"}]})]).answer,
          "内容")

    # 增量字符重复不能被"快照启发式"吃掉（实测 1+1 曾变成 11）
    check("重复字符的增量不丢字",
          stream([d({"p": "response/fragments/-1/content", "o": "APPEND",
                     "v": "1"}),
                  d({"v": "1"})]).answer, "11")

    # ★ 联网检索时还会出现 TOOL_SEARCH / TOOL_OPEN 这类**工具片段**，
    #   它们也带 content（"搜索到 10 个网页"就是），但那是界面状态不是答案。
    #   同时 BATCH 子事件用的是**相对路径**（"content"），必须拼上父路径，
    #   否则它会退化成普通正文路径、把工具状态混进答案。
    s6 = stream([
        d({"v": {"response": {"fragments": [
            {"id": 2, "type": "THINK", "content": "想"}]}}}),
        d({"p": "response", "o": "BATCH", "v": [
            {"p": "fragments", "o": "APPEND", "v": [
                {"id": 3, "type": "TOOL_SEARCH", "content": None,
                 "queries": [{"query": "永城 天气"}]}]}]}),
        d({"p": "response/fragments/-1", "o": "BATCH", "v": [
            {"p": "status", "v": "FINISHED"},
            {"p": "content", "v": "搜索到 10 个网页"}]}),
        d({"p": "response/fragments", "o": "APPEND", "v": [
            {"id": 4, "type": "RESPONSE", "content": "阴"}]}),
        d({"v": "转多云"}),
    ])
    check("工具片段内容不进正文", (s6.thinking, s6.answer), ("想", "阴转多云"))

    # 检索结果列表（results）不是内容，不该当成片段登记
    s7 = stream([
        d({"p": "response/fragments/-1/results", "o": "SET", "v": [
            {"url": "https://x", "title": "标题", "snippet": "摘要"}]}),
    ])
    check("检索结果列表不污染正文", (s7.thinking, s7.answer), ("", ""))

    print()
    print("=== 答案净化（直连带回的角标 / 状态行）===")
    from core.adapter import strip_noise
    check("方括号角标被清掉",
          strip_noise("体感偏凉[reference:0]。"), "体感偏凉。")
    check("多种角标都清掉",
          strip_noise("最高19[reference:3]，最低13[citation:1]。"),
          "最高19，最低13。")
    check("检索状态行被清掉",
          strip_noise("搜索到 10 个网页永城市今天阴。"), "永城市今天阴。")
    # DeepSeek 的免责声明是**不带换行**黏在最后一句后面的，按行丢不掉
    check("末尾免责声明被清掉",
          strip_noise("建议每天喝一小碗。本回答由 AI 生成，内容仅供参考，请仔细甄别"),
          "建议每天喝一小碗。")
    check("正常提到 AI 的正文不受影响",
          strip_noise("AI 生成的图片版权归属仍有争议。"),
          "AI 生成的图片版权归属仍有争议。")

    print()
    print("=== 回退契约（HTTP 失败必须让路给浏览器）===")
    a = pkg.get("deepseek")
    check("deepseek 声明支持直连", a.http_capable, True)

    # ★ 这几项必须**屏蔽掉真实凭证**再测。否则一旦本机已经收过凭证，
    #   就会真的把请求发出去：环境一变结论就变，测试也就失去了意义
    #   （而且会白消耗账号额度）。
    from unittest import mock
    import asyncio
    with mock.patch.object(H, "load_credentials", lambda: {}):
        r = asyncio.run(a.ask_http("测试", thread="__selftest__"))
    check("无凭证 → 返回 None", r, None)

    r2 = asyncio.run(a.ask_http("画张图", grab_images=True))
    check("生图 → 跳过 HTTP", r2, None)

    conf_off = {"http": {"enabled": False}}
    with mock.patch.object(a.__class__, "conf", property(lambda self: conf_off)):
        r3 = asyncio.run(a.ask_http("测试", thread="__selftest__"))
    check("站点关闭 → 返回 None", r3, None)

    print()
    print("=== 开关 ===")
    check("默认开启", H.enabled({}), True)
    check("站点可关", H.enabled({"http": {"enabled": False}}), False)

    print()
    print("=== 建会话**不能**带 PoW（带了就 INVALID_TARGET_PATH）===")

    class FakeResp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode()

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen: list[tuple[str, str]] = []

    def fake_post(path, body, cred, *, pow_header="", timeout=20.0):
        seen.append((path, pow_header))
        return FakeResp({"data": {"biz_data": {"id": "sess-abc"}}})

    from unittest import mock
    with mock.patch.object(H, "_post", fake_post):
        sid = H.create_session({"cookie": "x"})
    check("建会话拿到 id", sid, "sess-abc")
    check("建会话请求头无 PoW", [p for _, p in seen], [""])
    check("建会话打到正确端点",
          [p for p, _ in seen], ["/api/v0/chat_session/create"])

    print()
    print("=== 会话持久化（★ 跨进程必须读得回来）===")
    # 这里守的是一个极其隐蔽的 bug：_load_sessions() 少写 `global _sessions`
    # 时，加载结果只落到局部变量上，模块级 _sessions 永远是空的。
    # 单进程内完全看不出来（remember_session 是原地 mutate，改的是真家伙），
    # 但**网关一重启，所有续接静默失效** —— 之后每次追问都重开会话、
    # 回填整段历史，恰好把直连该省的 token 又花回去。
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "sess.json"
    tmp.write_text(json.dumps({"t1": {"session_id": "abc-123", "parent": 7}},
                              ensure_ascii=False), encoding="utf-8")
    # 模拟"刚启动的进程"：内存缓存清空、已加载标志复位
    with mock.patch.object(H, "SESSION_FILE", tmp), \
            mock.patch.object(H, "_sessions", {}), \
            mock.patch.object(H, "_sessions_loaded", False):
        got = H.session_for("t1")
    check("新进程能从磁盘读回会话", got.get("session_id"), "abc-123")
    check("会话里的 parent 也在", got.get("parent"), 7)

    print()
    if fails:
        print(f"✗ {len(fails)} 项失败：{'、'.join(fails)}")
        raise SystemExit(1)
    print("✓ 全部通过")


def replay(path: str) -> None:
    """把抓下来的原始 SSE 流回放给解析器 —— 不联网、可重复、不耗额度。

    排障姿势：先用 tools/probe_sse.py 抓一份 data/sse_raw.txt，
    之后改解析器就在这里反复回放。比每改一次都真去问一遍快得多，
    也不会白消耗账号额度。
    """
    from core.http_deepseek import _Stream
    from core.adapter import strip_noise

    p = Path(path)
    if not p.exists():
        raise SystemExit(f"找不到原流文件：{p}\n"
                         f"先用 tools/probe_sse.py \"随便问一句\" 抓一份。")

    st = _Stream()
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        # probe 落盘的格式是 "[序号] {json}"，这里把序号剥掉
        raw = line.split("] ", 1)[1] if line.startswith("[") else line
        if not raw.strip():
            continue
        n += 1
        st.feed("data: " + raw)

    print(f"回放 {n} 条事件")
    print(f"片段表  : {st.frag_types}")
    print(f"片段顺序: {st.frag_order}")
    print(f"消息 id : {st.message_id}")
    print()
    print(f"思考 {len(st.thinking)} 字：{st.thinking[:120]!r}")
    print()
    print(f"正文 {len(st.answer)} 字（净化前）：{st.answer[:120]!r}")
    print()
    print(f"正文（净化后，{len(strip_noise(st.answer))} 字）：")
    print(strip_noise(st.answer))


def main() -> None:
    ap = argparse.ArgumentParser(description="验证 DeepSeek 纯 HTTP 直连")
    ap.add_argument("--status", action="store_true", help="只看状态")
    ap.add_argument("--harvest", action="store_true",
                    help="开一次浏览器收凭证（Bearer 必须这么拿）")
    ap.add_argument("--ask", metavar="PROMPT", help="真的问一次")
    ap.add_argument("--selftest", action="store_true",
                    help="离线回归测试（不联网、不开浏览器）")
    ap.add_argument("--replay", metavar="FILE", nargs="?", const="",
                    help="回放抓好的原始 SSE 流（默认 data/sse_raw.txt）")
    ap.add_argument("--thread", default="http-test",
                    help="话题 id，同一 id 的追问会接上文（默认 http-test）")
    ap.add_argument("--reset", action="store_true", help="强制新开会话")
    args = ap.parse_args()

    if args.selftest:
        selftest()
    elif args.replay is not None:
        replay(args.replay or str(settings.DATA_DIR / "sse_raw.txt"))
    elif args.harvest:
        asyncio.run(harvest())
    elif args.ask:
        asyncio.run(ask(args.ask, args.thread, args.reset))
    else:
        show_status()


if __name__ == "__main__":
    main()
