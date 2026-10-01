"""用一次真实的 MCP stdio 握手，确认桥能挂进客户端。

用法：
    python tools/mcp_probe.py            # 不需要网关在跑也能验证协议层

通过 = 退出码 0；任何一步没在限时内完成 = 退出码 1。

---

## 这个脚本以前是坏的，坏法很值得记一笔（2026-10-01 修）

旧版核心是这一句：

    while time.time() - t0 < deadline and len(lines) < 12:
        line = proc.stdout.readline()        # ← 阻塞调用，没有超时

`readline()` 会一直阻塞到有一行可读为止，而 `deadline` 只在**两次读之间**
才被重新判断。桥一共只吐 5 行左右（initialize / tools/list / tools/call 的响应
加零星通知），读完之后第 6 次 `readline()` 就永久阻塞 —— 循环条件再也轮不到，
`deadline` 形同虚设。

更坏的是它把结果**攒到 `proc.terminate()` 之后**才统一打印。于是"卡住"的
表现就是**一个字都不输出**，看着就像"桥挂了"。一个本该省时间的诊断工具，
反而会把人骗去查根本不存在的问题 —— 实测就是这样：脚本无输出被杀，
而桥本身完全正常（8 个工具、真实返回）。

第二个隐患：`stderr=subprocess.PIPE` 开了却没人读。桥往 stderr 写得多了，
管道缓冲区填满 → 桥阻塞在 write → 双向死锁。**PIPE 开了就必须有人排空。**

第三个：`判定: FAIL` 时函数返回 `None`，进程退出码还是 0，CI/脚本会误判成功。

现在的做法：读线程把 stdout / stderr 都排进队列，每一步单独限时，
并且**每完成一步就立刻打印**（真卡住时能一眼看出卡在哪一步）。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# 期望注册的工具。少一个就说明 mcp_bridge 和 core 的接口对不上了。
EXPECTED_TOOLS = [
    "ask_free_llm", "fanout_ask", "stop_offload", "resume_offload",
    "analyze_task_offload", "list_free_providers", "savings_report",
    "grab_images", "close_windows",
]

# 每一步的等待上限（秒）。桥是本地进程，正常应在毫秒级响应；
# 留这么宽只是为了容忍首次 import 和联网查登录态。
T_INIT = 30.0
T_TOOLS = 30.0
T_CALL = 45.0


def _drain(stream, q: "queue.Queue[str]") -> None:
    """把一个流持续读进队列。**必须起线程**，否则阻塞读会拖住主流程。"""
    try:
        for line in stream:
            q.put(line.rstrip("\n"))
    except Exception:
        pass


class Bridge:
    def __init__(self) -> None:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"     # 别往技能目录里写 .pyc
        self.proc = subprocess.Popen(
            [PY, "-B", str(ROOT / "mcp_bridge.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT), env=env,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.out: queue.Queue[str] = queue.Queue()
        self.err: queue.Queue[str] = queue.Queue()
        threading.Thread(target=_drain, args=(self.proc.stdout, self.out),
                         daemon=True).start()
        threading.Thread(target=_drain, args=(self.proc.stderr, self.err),
                         daemon=True).start()

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def wait_for_id(self, want: int, seconds: float) -> dict | None:
        """等到 id 匹配的响应；期间收到的通知原样打出来。超时返回 None。"""
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                line = self.out.get(timeout=0.3)
            except queue.Empty:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                print(f"      [stdout 不是 JSON] {line[:160]}")
                continue
            if msg.get("id") == want:
                return msg
            if "id" not in msg:
                print(f"      [通知] {json.dumps(msg, ensure_ascii=False)[:120]}")
        return None

    def dump_stderr(self, tag: str, limit: int = 20) -> None:
        lines: list[str] = []
        while True:
            try:
                lines.append(self.err.get_nowait())
            except queue.Empty:
                break
        if lines:
            print(f"      --- {tag} 期间的 stderr（共 {len(lines)} 行，末 {min(limit, len(lines))} 行）---")
            for ln in lines[-limit:]:
                print("      |", ln)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def handshake() -> int:
    print("=" * 62)
    print("MCP stdio 握手（桥 = mcp_bridge.py）")
    print("=" * 62)

    b = Bridge()
    try:
        # ---- 1/4 initialize
        print("\n[1/4] initialize …", flush=True)
        b.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "mcp_probe", "version": "1"}}})
        r1 = b.wait_for_id(1, T_INIT)
        if r1 is None:
            print(f"      ✗ {T_INIT:.0f}s 内无响应")
            b.dump_stderr("initialize")
            return 1
        si = (r1.get("result") or {}).get("serverInfo") or {}
        print(f"      ✓ serverInfo = {si}")

        # ---- 2/4 完成初始化
        print("\n[2/4] notifications/initialized …", flush=True)
        b.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.3)
        print("      ✓ 已发送")

        # ---- 3/4 tools/list
        print("\n[3/4] tools/list …", flush=True)
        b.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        r2 = b.wait_for_id(2, T_TOOLS)
        if r2 is None:
            print(f"      ✗ {T_TOOLS:.0f}s 内无响应")
            b.dump_stderr("tools/list")
            return 1
        tools = (r2.get("result") or {}).get("tools") or []
        names = [t.get("name") for t in tools]
        print(f"      ✓ 注册了 {len(tools)} 个工具：{names}")
        missing = [n for n in EXPECTED_TOOLS if n not in names]
        if missing:
            print(f"      ✗ 少了这些预期工具：{missing}")
            return 1
        extra = [n for n in names if n not in EXPECTED_TOOLS]
        if extra:
            print(f"      ℹ 另有未在预期清单里的工具（不一定是错）：{extra}")

        # ---- 4/4 真调一个只读工具
        print("\n[4/4] tools/call list_free_providers …", flush=True)
        b.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "list_free_providers", "arguments": {}}})
        r3 = b.wait_for_id(3, T_CALL)
        if r3 is None:
            print(f"      ✗ {T_CALL:.0f}s 内无响应")
            b.dump_stderr("tools/call")
            return 1
        result = r3.get("result") or {}
        if r3.get("error") or result.get("isError"):
            print(f"      ✗ 调用报错：{json.dumps(r3, ensure_ascii=False)[:300]}")
            b.dump_stderr("tools/call")
            return 1
        txt = "".join(c.get("text", "") for c in (result.get("content") or [])
                      if c.get("type") == "text")
        print(f"      ✓ 返回 {len(txt)} 字：{txt[:200].replace(chr(10), ' / ')}")
        if not txt.strip():
            print("      ✗ 返回为空")
            return 1
        # 网关没在跑时，桥会明确回「调用失败：…请确认 Token Saver 服务已启动（…）」。
        # 那也算协议层通过 —— 本脚本验的是"桥能挂进客户端"，不是"网关活着"。
        # 文案取自 mcp_bridge.py 的 `except httpx.HTTPError` 分支，改那边记得同步这里。
        if "服务已启动" in txt or "调用失败" in txt:
            print("      ℹ 网关当前没在跑；协议层已通过（起网关后本步会返回真实站点列表）")

        b.dump_stderr("收尾")
        print("\n" + "=" * 62)
        print("结果：全部通过 ✓")
        print("=" * 62)
        return 0
    finally:
        b.close()


if __name__ == "__main__":
    sys.exit(handshake())
