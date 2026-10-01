"""DeepSeek 网页端 PoW 求解器（纯 HTTP 路线用）。

背景
----
DeepSeek 的 /api/v0/chat/completion 被两道闸门把着：

    1. 需要一个已登录的会话（Cookie + Bearer）
    2. 每个请求都要带 x-ds-pow-response 头 —— 内容是服务器下发的
       工作量证明挑战的解答

第二道闸门是纯计算题，不需要浏览器。协议（逆向自官方前端）：

    POST /api/v0/chat/create_pow_challenge
      body: {"target_path": "/api/v0/chat/completion"}
      → {"data": {"biz_data": {"challenge": {
             "algorithm": "DeepSeekHashV1",
             "challenge": "<hex>",
             "salt":       "<hex>",
             "difficulty": 144000,
             "expire_at":  1777057596443,
             "signature":  "<hex>"
         }}}}

    answer = wasm_solve(challenge, f"{salt}_{expire_at}", difficulty)

    x-ds-pow-response = base64(JSON.stringify({
        ...challenge, answer, target_path: "/api/v0/chat/completion"
    }))

这个模块跑 DeepSeek 官方那份 sha3 WASM（tools/extract_sha3_wasm.py
从 openclaw-zero-token 里抽出来的），所以算力和网页端完全一致。

**它只是可选加速路径**：拿不到模型时自动退回浏览器方案，不影响现有功能。
"""
from __future__ import annotations

import base64
import json
import struct
import threading
import time
from pathlib import Path
from typing import Any

# WASM 是**随包分发的静态资源**（26 KB），不放运行期目录：
# 放在 assets/ 里，覆盖安装时不会丢；老位置 data/ 仍保留作兜底。
from . import settings

WASM_PATH = settings.ASSETS_DIR / "sha3_wasm.wasm"
if not WASM_PATH.exists():                       # 兼容早期把 wasm 放在 data/ 的版本
    _legacy = settings.DATA_DIR / "sha3_wasm.wasm"
    if _legacy.exists():
        WASM_PATH = _legacy

# 各站点的 API 端点
DS_BASE = "https://chat.deepseek.com"
POW_CHALLENGE_PATH = "/api/v0/chat/create_pow_challenge"

_wasm_lock = threading.Lock()


class PowUnavailable(RuntimeError):
    """拿不到 / 跑不动 WASM 求解器。调用方应退回浏览器路线。"""


class _Solver:
    """把 WASM 实例 + 它的线性内存一次性建好，之后复用。

    wasmtime 的 Store 不是可重入的，所以用锁把并发的求解请求串起来 ——
    跟参考实现（davoodya/Deepseek-API-Free-Usage）的做法一致：
    吞吐是串行的，这是这类 PoW 的固有性质，不是缺陷。
    """

    def __init__(self) -> None:
        self._store = None
        self._instance = None
        self._memory = None

    def _ensure(self) -> None:
        if self._instance is not None:
            return
        if not WASM_PATH.exists():
            raise PowUnavailable(
                f"缺少 WASM 模块：{WASM_PATH}\n"
                f"用 tools/extract_sha3_wasm.py 从参考仓库里抽一份出来。")
        try:
            from wasmtime import Instance, Module, Store
        except ImportError as e:  # noqa: BLE001
            raise PowUnavailable(
                "未安装 wasmtime。装它即可启用纯 HTTP 加速：\n"
                "  pip install wasmtime") from e

        store = Store()
        module = Module.from_file(store.engine, str(WASM_PATH))
        # 该模块是 wasm-bindgen 产物，除了自带的 import 外还需要 wbg 命名空间
        linker_imports = {}
        try:
            inst = Instance(store, module, [])
        except Exception:  # noqa: BLE001
            # 退一步：显式构造 wbg 空对象供 import
            from wasmtime import Func, FuncType, ValType

            def _noop(*_a: Any) -> None:
                return None

            for imp in module.imports:
                if imp.module == "wbg":
                    linker_imports[imp.name] = Func(
                        store, FuncType([], []), _noop)
            inst = Instance(store, module, [linker_imports.get(i.name) for i in module.imports])

        self._store, self._instance = store, inst
        ex = inst.exports(store)
        self._memory = ex["memory"]
        self._wasm_solve = ex["wasm_solve"]
        self._add_to_stack = ex["__wbindgen_add_to_stack_pointer"]
        self._malloc = ex["__wbindgen_export_0"]

    # ------------------------------------------------ WASM 内存小工具
    def _write(self, ptr: int, data: bytes) -> None:
        """往线性内存写字节。**每次都重新取 data_ptr**。

        ★ 这是整个模块最容易踩的坑：WASM 的 `memory.grow` 会重新分配底层
        buffer，之前拿到的 data_ptr 立刻失效（wasmtime 里表现为写入
        落到一片陈旧内存上，读回来是 0）。实测第二次 malloc 就会触发
        grow（17→18 页），于是"先 malloc 两个指针、再统一写入"的写法
        必然失败 —— 求解器会一路算到上限返回 status=0，
        报出来却是"挑战可能已过期"，极具误导性。
        所以分配和写入必须成对、且写入前重新取指针。
        """
        mem = self._memory.data_ptr(self._store)
        for i, b in enumerate(data):
            mem[ptr + i] = b

    def _encode(self, s: str) -> tuple[int, int]:
        data = s.encode("utf-8")
        ptr = self._malloc(self._store, len(data), 1)
        self._write(ptr, data)
        return ptr, len(data)

    def solve(self, challenge: str, salt: str, expire_at: int,
              difficulty: int) -> int:
        self._ensure()
        with _wasm_lock:
            store = self._store
            # ★ 前缀必须**以 _ 结尾**：f"{salt}_{expire_at}_"。
            #   漏掉最后那个下划线，WASM 会一路算到上限也找不到解、
            #   返回 status=0。这个细节任何一处都不能省。
            prefix = f"{salt}_{expire_at}_"
            stack = self._add_to_stack(store, -16)
            try:
                cptr, clen = self._encode(challenge)
                pptr, plen = self._encode(prefix)
                self._wasm_solve(store, stack, cptr, clen, pptr, plen,
                                 float(difficulty))
                mem = self._memory.data_ptr(store)
                status = int.from_bytes(bytes(mem[stack:stack + 4]), "little",
                                        signed=True)
                answer = struct.unpack(
                    "<d", bytes(mem[stack + 8:stack + 16]))[0]
            finally:
                # ★ 无论成败都要把栈指针还回去。只写成功分支的话，
                #   每次失败都会永久漏掉 16 字节栈空间 —— 实测连续几次
                #   失败后 retptr 一路 1048560→1048544→1048528 递减，
                #   最终写出界把 WASM 内部状态踩坏，之后连本来能解的
                #   挑战也一律返回 0，且报错信息是误导性的"挑战可能已过期"。
                self._add_to_stack(store, 16)
            if status == 0:
                raise PowUnavailable(
                    "WASM 求解器未找到解。可能原因：\n"
                    "  · 挑战已过期（expire_at 过了）—— 重新取一次即可\n"
                    "  · challenge / salt / difficulty 不是服务端下发的原配三元组\n"
                    "    （三者有内部一致性校验，自己拼的必定解不出）")
            return int(answer)


_solver = _Solver()


def solve_pow(challenge: dict, target_path: str) -> str:
    """解一个 DeepSeek PoW 挑战，返回可直接塞进请求头的字符串。"""
    algorithm = challenge.get("algorithm") or ""
    if algorithm != "DeepSeekHashV1":
        raise PowUnavailable(f"未知的 PoW 算法：{algorithm!r}")

    t0 = time.time()
    answer = _solver.solve(
        challenge=challenge["challenge"],
        salt=challenge["salt"],
        expire_at=int(challenge.get("expire_at") or 0),
        difficulty=int(challenge.get("difficulty") or 0),
    )
    payload = dict(challenge)
    payload["answer"] = answer
    payload["target_path"] = target_path
    blob = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode()
    print(f"[pow] {target_path} 解出 answer={answer}"
          f"（{time.time() - t0:.2f}s，difficulty={challenge.get('difficulty')}）",
          flush=True)
    return blob


def available() -> tuple[bool, str]:
    """求解器是否可用。返回 (可用?, 原因)。"""
    try:
        _solver._ensure()
        return True, "ok"
    except PowUnavailable as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def selfcheck() -> bool:
    """自检：WASM 能加载吗？用官方样例三元组跑通一遍。

    注意样例里的 challenge/salt/difficulty 是**配套**的，三者必须原样一起用
    （WASM 内部有一致性校验，自己乱配必定解不出）。所以自检只证明
    "模块加载 + 调用约定正确"，不代表能解任意挑战 —— 真正的验证得打真接口。
    """
    ok, why = available()
    print(f"1) WASM 加载: {'ok' if ok else '失败 —— ' + why}")
    if not ok:
        return False

    sample = {
        "algorithm": "DeepSeekHashV1",
        "challenge": "b0000b22959bad0cc1ecbbfa07f97191b20332fa10d7341ff9c7ba6e7ed927f1",
        "salt": "dde3ed472be5a2494ee0",
        "difficulty": 65536,
        "expire_at": 1777057596443,
        "signature": "selftest",
    }
    t0 = time.time()
    try:
        blob = solve_pow(sample, "/api/v0/chat/completion")
    except PowUnavailable as e:
        print(f"2) 求解: 失败 —— {e}")
        return False
    d = json.loads(base64.b64decode(blob))
    print(f"2) 求解: ok（{time.time() - t0:.3f}s，answer={d['answer']}）")

    need = {"algorithm", "challenge", "salt", "answer", "signature",
            "target_path", "difficulty", "expire_at"}
    missing = need - set(d)
    print(f"3) 响应字段: {'ok' if not missing else '缺 ' + str(missing)}")

    # 连跑多次，确认栈指针没被漏掉（这是实测踩过的坑）
    try:
        for _ in range(5):
            solve_pow(sample, "/api/v0/chat/completion")
        print("4) 连续 5 次复用: ok（栈指针无泄漏）")
    except PowUnavailable as e:
        print(f"4) 连续复用: 失败 —— {e}")
        return False
    return True


def fetch_challenge(cookie: str, bearer: str = "",
                    target_path: str = "/api/v0/chat/completion",
                    timeout: float = 15.0) -> dict:
    """向 DeepSeek 要一个真实的 PoW 挑战。

    这是唯一能真正验证求解器的路子 —— 挑战必须由服务端签发。
    需要一个已登录会话的 cookie（Bearer 可选，有则一起带上）。
    """
    import urllib.request

    body = json.dumps({"target_path": target_path}).encode()
    req = urllib.request.Request(
        DS_BASE + POW_CHALLENGE_PATH, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Cookie": cookie,
            "Referer": DS_BASE + "/",
            "Origin": DS_BASE,
            "x-client-platform": "web",
            "x-client-version": "1.7.0",
            "x-app-version": "20241129.1",
            "x-client-locale": "zh_CN",
            "x-client-timezone-offset": "28800",
            **({"Authorization": f"Bearer {bearer}"} if bearer else {}),
        })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    biz = (data.get("data") or {}).get("biz_data") or {}
    challenge = biz.get("challenge") or data.get("challenge")
    if not challenge:
        raise PowUnavailable(f"响应里没有 challenge 字段：{str(data)[:200]}")
    return challenge


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--live":
        # 打真接口验证：python core/pow_deepseek.py --live <cookie> [bearer]
        cookie = sys.argv[2] if len(sys.argv) > 2 else ""
        bearer = sys.argv[3] if len(sys.argv) > 3 else ""
        ch = fetch_challenge(cookie, bearer)
        print(f"拿到挑战：algorithm={ch.get('algorithm')} "
              f"difficulty={ch.get('difficulty')} "
              f"expire_at={ch.get('expire_at')}")
        t0 = time.time()
        blob = solve_pow(ch, "/api/v0/chat/completion")
        print(f"求解成功（{time.time() - t0:.2f}s），响应 {len(blob)} 字节")
    else:
        raise SystemExit(0 if selfcheck() else 1)
