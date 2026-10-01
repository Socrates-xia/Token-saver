"""DeepSeek 纯 HTTP 直连（不开浏览器）。

为什么要有它
------------
浏览器路线每次都要起 Edge、加载页面，出第一个字要 7~15 秒。这笔固定开销
把「什么任务值得外包」的盈亏平衡点顶到了 195 token —— 很多本该外包的
小活儿因为这十几秒等待而变得不划算。

DeepSeek 的网页接口其实只有两道闸门：
    1. 已登录的会话（Cookie + Bearer）
    2. 每个请求的 PoW 挑战（纯计算题，见 core/pow_deepseek.py）
第二道在上一轮已经打通。这里把第一道也接上，于是整个取答案过程
**完全不需要浏览器**，往返降到 1~2 秒。

凭证从哪来
----------
Cookie 和 Bearer 都取自**你已经登录的那个浏览器 profile**：
    · Cookie  —— Playwright 的 storage_state（browser.save_session 已在存）
    · Bearer  —— 页面 localStorage 里的 token（只能从活着的页面读）
所以第一次仍然要开一次浏览器（顺便把 Bearer 收下来存盘），之后就是纯 HTTP。
Bearer 过期（HTTP 报 401）时自动作废缓存，退回浏览器路线，
那条路会顺手把新凭证收回来 —— 自愈，不需要人工介入。

会话连续性
----------
HTTP 路线有自己的会话概念：chat_session_id + parent_message_id。
把一个话题的这两个值存起来，追问就能**不重发历史**地接上，
和浏览器路线的 reuse_page_session 是同一个目标。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import pow_deepseek, settings

BASE = "https://chat.deepseek.com"
CRED_FILE = settings.DATA_DIR / "state" / "deepseek_http.json"
SESSION_FILE = settings.DATA_DIR / "state" / "deepseek_http_sessions.json"

# 浏览器里那份 storage_state（browser.save_session 存的）
WA_STATE_FILE = settings.DATA_DIR / "state" / "deepseek.json"

CLIENT_HEADERS = {
    "x-client-platform": "web",
    "x-client-version": "1.7.0",
    "x-app-version": "20241129.1",
    "x-client-locale": "zh_CN",
    "x-client-timezone-offset": "28800",
}
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class HttpAuthError(RuntimeError):
    """凭证过期/无效 —— 调用方应作废缓存并退回浏览器。"""


class HttpError(RuntimeError):
    """其他 HTTP 层面失败 —— 同样退回浏览器，但不作废凭证。"""


class HttpStopped(HttpError):
    """用户在生成过程中叫停了本次调用 —— **这是意图，不是故障**。

    为什么值得单独开一个类：`_stream_completion` 每读到一行 SSE 就查一次
    `should_stop()`，命中即抛异常、顺手断开连接。若沿用 HttpError，
    调用方那两个 `except Exception` 会把它吞成"直连失败 → 退回浏览器"，
    日志里看着像 HTTP 通道坏了 —— 而实际什么都没坏，是用户按了停机。
    （真机实测 2026-10-01，日志确实这么撒的谎。）

    见到这个类应当**直接收手**：不换站点、不退浏览器、不重试。
    """


# ------------------------------------------------------------------ 凭证
def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_json(p: Path, data: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _cookies_from_storage_state() -> str:
    """把 Playwright 存下来的 storage_state 转成 Cookie 头。

    这样即使浏览器正关着，也能拿到 Cookie —— Bearer 才必须靠活页面。
    """
    st = _load_json(WA_STATE_FILE)
    parts: list[str] = []
    for c in st.get("cookies") or []:
        name, val = c.get("name"), c.get("value")
        dom = c.get("domain") or ""
        if not name or val is None:
            continue
        if "deepseek" not in dom:
            continue
        parts.append(f"{name}={val}")
    return "; ".join(parts)


def load_credentials() -> dict:
    """读缓存凭证。返回 {} 表示还没有/已作废。

    没有专门的凭证文件时，退一步**用浏览器存下来的登录态**兜底；
    此时 cookie 有、bearer 缺。好处是：从没跑过 harvest 的机器上，
    第一次调用就能直接试 HTTP（万一站点认 cookie 就省了一次开浏览器），
    不成也不过是 401 → 作废 → 退回浏览器，没有副作用。
    """
    cred = _load_json(CRED_FILE)
    if cred.get("stale"):
        return {}
    if cred.get("cookie"):
        return cred
    # 兜底：从 storage_state 里扒 cookie
    cookie = _cookies_from_storage_state()
    if cookie:
        return {"cookie": cookie, "bearer": "", "user_agent": DEFAULT_UA,
                "saved_at": 0, "stale": False, "from_storage_state": True}
    return {}


def invalidate(reason: str = "") -> None:
    """作废缓存。下次会退回浏览器并借机重新收集。"""
    cred = _load_json(CRED_FILE)
    if cred:
        cred["stale"] = True
        cred["stale_reason"] = reason
        cred["stale_at"] = time.time()
        _save_json(CRED_FILE, cred)
    if reason:
        print(f"[http] 凭证已作废（{reason}），将退回浏览器", flush=True)


# 凭证多久重收一次。太频繁没必要（每次多一次 localStorage 读取），
# 太久又可能拿着过期的 bearer 白试一轮 HTTP。
_HARVEST_TTL = 1800.0


def needs_harvest() -> bool:
    """现在需要（重新）收集凭证吗？

    浏览器路线每次都会问一次，所以这个判断要足够便宜、也要足够准：
    没凭证 / 已作废 / 那份 bearer 是空的 / 存太久了 → 该收。
    """
    cred = _load_json(CRED_FILE)
    if not cred or cred.get("stale"):
        return True
    if not cred.get("cookie"):
        return True
    if not cred.get("bearer"):
        return True
    if time.time() - float(cred.get("saved_at") or 0) > _HARVEST_TTL:
        return True
    return False


async def harvest(page) -> dict:
    """从**活着的**页面收下 Cookie + Bearer 并存盘。

    在浏览器路线成功跑过一次之后调用最省事 —— 页面已经就绪，
    这里只是多一次 localStorage 读取，成本可以忽略。
    """
    cookie = ""
    try:
        cookies = await page.context.cookies(
            ["https://chat.deepseek.com", "https://deepseek.com"])
        cookie = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
    except Exception as e:  # noqa: BLE001
        print(f"[http] 读 cookie 失败：{type(e).__name__}: {e}", flush=True)

    if not cookie:
        cookie = _cookies_from_storage_state()

    bearer = ""
    ua = DEFAULT_UA
    try:
        ua = await page.evaluate("() => navigator.userAgent") or DEFAULT_UA
    except Exception:  # noqa: BLE001
        pass
    try:
        ls = await page.evaluate("""() => {
            const out = {};
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                if (k) out[k] = localStorage.getItem(k) || '';
            }
            return out;
        }""")
        bearer = _pick_bearer(ls)
    except Exception as e:  # noqa: BLE001
        print(f"[http] 读 localStorage 失败：{type(e).__name__}: {e}",
              flush=True)

    cred = {
        "cookie": cookie,
        "bearer": bearer,
        "user_agent": ua,
        "saved_at": time.time(),
        "stale": False,
    }
    _save_json(CRED_FILE, cred)
    print(f"[http] 已收下凭证：cookie {len(cookie)} 字节、"
          f"bearer {'有' if bearer else '无'}", flush=True)
    return cred


def _pick_bearer(ls: dict) -> str:
    """从 localStorage 里挑出 token。

    DeepSeek 把它存成 {"value": "...", "__version": "0"} 这种带壳的 JSON，
    直接把整串塞进 Authorization 头会 401 —— 必须剥壳取里面的 value。
    """
    for key, raw in (ls or {}).items():
        k = (key or "").lower()
        if "token" not in k and "auth" not in k:
            continue
        if not isinstance(raw, str) or len(raw) < 10:
            continue
        try:
            obj = json.loads(raw)
        except Exception:  # noqa: BLE001
            if len(raw) > 20:
                return raw
            continue
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            for field in ("value", "token", "access_token", "accessToken"):
                v = obj.get(field)
                if isinstance(v, str) and len(v) > 10:
                    return v
    return ""


# ------------------------------------------------------------------ HTTP
def _headers(cred: dict, *, with_pow: str = "") -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Cookie": cred.get("cookie") or "",
        "User-Agent": cred.get("user_agent") or DEFAULT_UA,
        "Referer": BASE + "/",
        "Origin": BASE,
        **CLIENT_HEADERS,
    }
    if cred.get("bearer"):
        h["Authorization"] = f"Bearer {cred['bearer']}"
    if with_pow:
        h["x-ds-pow-response"] = with_pow
    return h


def _post(path: str, body: dict, cred: dict, *, pow_header: str = "",
          timeout: float = 20.0):
    req = urllib.request.Request(
        BASE + path, method="POST",
        data=json.dumps(body).encode(),
        headers=_headers(cred, with_pow=pow_header))
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_pow(challenge_path: str, cred: dict) -> str:
    """要一个挑战并解掉，返回可直接塞进请求头的字符串。"""
    with _post(pow_deepseek.POW_CHALLENGE_PATH,
               {"target_path": challenge_path}, cred) as resp:
        data = json.loads(resp.read().decode())
    biz = (data.get("data") or {}).get("biz_data") or {}
    ch = biz.get("challenge") or data.get("challenge")
    if not ch:
        raise HttpError(f"拿不到 PoW 挑战：{str(data)[:200]}")
    return pow_deepseek.solve_pow(ch, challenge_path)


def create_session(cred: dict) -> str:
    """新建一个远端会话。

    ★ 这个接口**不要**带 PoW。实测给它要挑战会被拒：
        {"biz_code": 1, "biz_msg": "INVALID_TARGET_PATH"}
    因为服务端只给 /api/v0/chat/completion 这类接口签挑战，
    chat_session/create 不在名单里。

    踩过的坑：一开始照搬 completion 的写法，先 fetch_pow 再请求，
    结果每次直连都在这里挂掉、静默退回浏览器 —— 现象是"HTTP 配好了
    却始终走浏览器"，日志里只有一句 INVALID_TARGET_PATH。
    所以下面还留了一手：万一哪天它真开始要 PoW 了，
    400/403 时自动带挑战重试一次。
    """
    path = "/api/v0/chat_session/create"
    try:
        with _post(path, {}, cred) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        # 万一现在要 PoW 了 —— 带挑战再试一次
        print(f"[http] 建会话裸请求被拒（HTTP {e.code}），"
              f"带 PoW 重试：{body}", flush=True)
        pow_header = fetch_pow(path, cred)
        with _post(path, {}, cred, pow_header=pow_header) as resp:
            data = json.loads(resp.read().decode())

    biz = (data.get("data") or {}).get("biz_data") or {}
    sess = biz.get("chat_session") or biz
    sid = sess.get("id") or sess.get("chat_session_id") or ""
    if not sid:
        raise HttpError(f"建会话失败：{str(data)[:200]}")
    return sid


# ------------------------------------------------------------------ SSE
# 片段内容的路径形如 response/fragments/-1/content。
# **-1 表示"当前活跃的那个片段"**，而不是某个固定片段 —— 思考和正文
# 会先后落在同一个 -1 上，所以光凭路径分不出归属，必须靠片段类型。
_FRAG_CONTENT_RE = re.compile(r"^response/fragments/(-?\d+)/content$")


class _Stream:
    """把 DeepSeek 的 SSE 增量拼成正文与思考过程。

    ★ 协议实况（抓真流确认，2026-10-01）
    ---------------------------------------------------------------
    DeepSeek 现在用的是**片段（fragment）模型**。整条流只有三类事件：

      1) 片段元数据 —— **没有 p 字段**，v 是一整个 response 对象：
           {"v": {"response": {"fragments": [
               {"id": 2, "type": "THINK", "content": "我们需要", ...}]}}}

      2) 显式路径事件 —— 宣告"接下来往哪儿写"：
           {"p": "response/fragments/-1/content", "o": "APPEND", "v": "回答"}

      3) **裸增量 —— 只有 v、没有 p**：
           {"v": "用户"}  {"v": "中文"}  {"v": "问题"}  …
         它承接**最近一次显式声明的路径**。这一条最容易漏：单看一行，
         裸增量长得就像"正文"，于是整段思考被安安静静倒进 answer 里。

    片段切换长这样：
        …思考增量… → {"p": "response/fragments/-1/elapsed_secs", "o":"SET", …}
        → {"p": "response/fragments", "o": "APPEND",
           "v": [{"id": 3, "type": "RESPONSE", "content": "1", …}]}
        → {"p": "response/fragments/-1/content", "v": "+"}
    也就是说**新片段是靠 `p=response/fragments` 追加进来的**，追加之后
    `-1` 才指向它。所以这条也必须登记进片段表，否则 `-1` 永远停在旧片段上，
    归属全错（现象就是"思考全进正文、正文只有零星几个字"）。

    ★ 四个坑，全都会"不报错、只出错"：
      (a) 类型字面量是 `THINK`，不是 THINKING / REASONING；
      (b) `-1` 对思考和正文是**同一个值**，只能靠片段类型判归属；
      (c) 裸增量必须沿用上一条显式路径，不能当正文；
      (d) 片段内容**一律是增量**，套用"累计快照"启发式会白吃掉字符
          （实测 "1" + "1" 被误判成快照，答案里的 "1+1" 变成 "11"）。

    还有两件开了联网检索才会出现的事：
      · **工具片段**：type 为 `TOOL_SEARCH` / `TOOL_OPEN`（不是
        THINK/RESPONSE）。它们也带 content —— "搜索到 10 个网页" 就是
        TOOL_SEARCH 的内容，属于界面状态而不是答案，必须整类丢弃。
      · **批处理 + 相对路径**：
          {"p": "response/fragments/-1", "o": "BATCH", "v": [
              {"p": "content", "o": "APPEND", "v": "[reference:0]"}]}
        子事件的 `"content"` 是相对路径，要拼上父路径才是
        `response/fragments/-1/content`。不拼的话它会退化成一个普通
        正文路径，工具片段的内容就混进答案了。
    """

    def __init__(self) -> None:
        self.answer = ""
        self.thinking = ""
        self.message_id: Any = None
        self.frag_types: dict[Any, str] = {}   # 片段 id -> 类型
        self.frag_order: list[Any] = []        # 片段出现顺序，解析 -1 要用
        self.cur_path = ""                     # 最近一次显式声明的内容路径

    # ------------------------------------------------------------ 归类
    @staticmethod
    def _bucket(ftype: str) -> str:
        """片段类型 → 归到哪个筐。返回 "" 表示**不该收**。

        实测会出现的类型：
            THINK      —— 思考过程
            RESPONSE   —— 正文
            TOOL_SEARCH / TOOL_OPEN —— 联网检索、打开网页这类**工具片段**。
                它们也带 content（例如 "搜索到 10 个网页"），但那是界面
                状态，不是答案。不单独剔掉的话，答案开头就会多一句
                "搜索到 10 个网页"。
        只认 "THINK" 做思考（"THINKING" 也含这个子串，一并覆盖）；
        TOOL* 丢弃；其余（含未知类型）当正文，宁可多收也不要漏收。
        """
        t = (ftype or "").upper()
        if "THINK" in t:
            return "thinking"
        if t.startswith("TOOL"):
            return ""
        return "answer"

    @staticmethod
    def _join(parent: str, p: str) -> str:
        """把（可能相对的）子路径拼成完整路径。

        ★ BATCH 里的子事件 p 是**相对**的：
            {"p": "response/fragments/-1", "o": "BATCH", "v": [
                {"p": "content", "v": "[reference:0]"}]}
         这里的 "content" 指的是 `response/fragments/-1/content`。
        不拼父路径的话，"content" 会被当成一个普通正文路径 → 工具片段的
        "搜索到 10 个网页" 就被安进正文了。
        """
        if not p:
            return parent or ""
        if not parent or p.startswith("response"):
            return p
        return parent.rstrip("/") + "/" + p

    @staticmethod
    def _is_content_path(p: str) -> bool:
        """这条路径算不算"内容"路径（值得当作后续裸增量的落点）。

        必须区分开：`response/status`、`…/elapsed_secs` 这类也会带 p，
        但它们**不能**顶替内容落点 —— 否则紧跟其后的裸增量就没地方去了。
        """
        if not p:
            return False
        if _FRAG_CONTENT_RE.match(p):
            return True
        if "reasoning" in p or "thinking" in p:
            return True
        return "content" in p or "choices" in p or p == "response"

    def _acc(self, bucket: str, chunk: str, op: str = "") -> None:
        if not chunk or not bucket:
            return
        cur = self.thinking if bucket == "thinking" else self.answer
        o = (op or "").upper()
        if o == "SET":
            new = chunk
        elif o == "APPEND":
            new = cur + chunk
        elif cur and chunk.startswith(cur):
            # 没标 op 时的**老协议**兜底：看着像累计快照就整体替换。
            # ★ 片段内容不能走这条 —— 真实服务端发的是增量，而这个启发式
            #   会把 "1" + "1" 误判成快照，白吃掉一个字符。
            new = chunk
        else:
            new = cur + chunk
        if bucket == "thinking":
            self.thinking = new
        else:
            self.answer = new

    def _frag_type(self, idx: int) -> str:
        """路径里的片段编号 → 类型。负数按"从末尾数"解释（-1 = 最新片段）。"""
        if not self.frag_order:
            return ""
        if idx < 0:
            try:
                fid = self.frag_order[idx]
            except IndexError:
                return ""
        else:
            fid = idx if idx in self.frag_types else (
                self.frag_order[idx] if idx < len(self.frag_order) else idx)
        return self.frag_types.get(fid, "")

    def _register(self, frags: list) -> None:
        """登记一批片段：记类型、记顺序，**只在首次见到时**收下它的 content。

        "首次才收"是必须的：同一条片段会被重发（内容已经变长了），
        再收一遍就是重复计数。
        """
        for f in frags:
            if not isinstance(f, dict):
                continue
            t = str(f.get("type") or "")
            c = f.get("content")
            fid = f.get("id")
            if fid is None:
                # 另一种形态：没有 id 的独立内容块，不参与 -1 解析
                if isinstance(c, str) and c:
                    self._acc(self._bucket(t), c, "APPEND")
                continue
            first = fid not in self.frag_types
            self.frag_types[fid] = t
            if fid not in self.frag_order:
                self.frag_order.append(fid)
            if first and isinstance(c, str) and c:
                # 片段的初始 content 就是它开头那段，按增量收下
                self._acc(self._bucket(t), c, "APPEND")

    def _scan_fragments(self, d: Any) -> None:
        """找出埋在任意嵌套里的 `fragments` 列表（元数据事件的形态）。"""
        found: list[list] = []

        def walk(o: Any) -> None:
            if isinstance(o, dict):
                fr = o.get("fragments")
                if isinstance(fr, list) and fr:
                    found.append(fr)
                for vv in o.values():
                    walk(vv)
            elif isinstance(o, list):
                for vv in o:
                    walk(vv)

        walk(d)
        for fr in found:
            self._register(fr)

    # ------------------------------------------------------------ 入口
    def feed(self, line: str) -> None:
        if not line.startswith("data: "):
            return
        raw = line[6:].strip()
        if not raw or raw == "[DONE]":
            return
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        if isinstance(d, dict):
            self._consume(d)
        elif isinstance(d, list):
            for sub in d:
                if isinstance(sub, dict):
                    self._consume(sub)

    def _route(self, p: str, v: str, op: str) -> bool:
        """按路径把一段文本归筐。返回是否已消费（False = 与内容无关，丢弃）。"""
        m = _FRAG_CONTENT_RE.match(p)
        if m:
            ftype = self._frag_type(int(m.group(1)))
            # ★ 片段内容一定是增量：没标 op 也当 APPEND（别走快照启发式）
            eff_op = op if op.upper() == "SET" else "APPEND"
            self._acc(self._bucket(ftype) if ftype else "answer", v, eff_op)
            return True
        if "reasoning" in p or "thinking" in p:
            self._acc("thinking", v, op)
            return True
        if "content" in p or "choices" in p or p == "response":
            self._acc("answer", v, op)
            return True
        return False

    def _consume(self, d: dict, parent: str = "") -> None:
        if d.get("response_message_id") is not None:
            self.message_id = d["response_message_id"]

        p_raw = d.get("p")
        p = self._join(parent, p_raw if isinstance(p_raw, str) else "")
        op = str(d.get("o") or "")
        v = d.get("v")

        # 批处理：一个事件里套一包子事件，按同一个状态机逐个过。
        # 子事件的 p 是**相对路径**，所以把本条路径当父路径传下去。
        if op.upper() == "BATCH" and isinstance(v, list):
            for sub in v:
                if isinstance(sub, dict):
                    self._consume(sub, parent=p)
            return

        # 先把片段元数据收进来 —— 后面判归属全靠它
        self._scan_fragments(d)

        if isinstance(v, list):
            # 只有 "…/fragments" 的列表才是片段；其余列表（检索结果
            # results 之类）是附属数据，不是内容。
            if not p or p.rsplit("/", 1)[-1] == "fragments":
                self._register(v)
            return

        if isinstance(v, str) and v:
            # ★ 没有 p 的裸增量，承接**上一条显式声明的路径**
            eff = p or self.cur_path
            if self._route(eff, v, op) and self._is_content_path(p):
                self.cur_path = p
            return

        # 老协议：type + content 字段
        t = str(d.get("type") or "").lower()
        c = d.get("content")
        if isinstance(c, str) and c:
            if "think" in t or "reason" in t:
                self._acc("thinking", c)
            elif t in ("text", "content", "response"):
                self._acc("answer", c)


def _stream_completion(cred: dict, *, session_id: str, parent_id: Any,
                       prompt: str, thinking: bool, search: bool,
                       timeout: float, should_stop: Callable[[], bool] | None,
                       on_first_token: Callable[[], None] | None = None) -> dict:
    """发一次 completion 并把 SSE 读完。**这是阻塞函数**，由线程池调用。"""
    path = "/api/v0/chat/completion"
    pow_header = fetch_pow(path, cred)
    body = {
        "chat_session_id": session_id,
        "parent_message_id": parent_id,
        "prompt": prompt,
        "ref_file_ids": [],
        "thinking_enabled": bool(thinking),
        "search_enabled": bool(search),
        "preempt": False,
    }
    st = _Stream()
    got_first = False
    with _post(path, body, cred, pow_header=pow_header, timeout=timeout) as resp:
        deadline = time.time() + timeout
        while True:
            if should_stop and should_stop():
                # ★ 抛 HttpStopped 而不是 HttpError：见该类的说明。
                raise HttpStopped("[已停机] 用户在生成过程中叫停了本次调用")
            if time.time() > deadline:
                break
            line = resp.readline()
            if not line:
                break
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            st.feed(line)
            if not got_first and (st.answer or st.thinking):
                got_first = True
                if on_first_token:
                    on_first_token()
    return {"answer": st.answer, "thinking": st.thinking,
            "message_id": st.message_id}


# ------------------------------------------------------------------ 对外
_sessions_loaded = False
_sessions: dict[str, dict] = {}


def _load_sessions() -> None:
    # ★ `global _sessions` 不能漏！
    #   漏了的话，下面那行赋值只是建了个**局部变量**，模块级的 _sessions
    #   永远是空的 —— 于是会话文件一直在写、却从来没被读回来过。
    #   现象极隐蔽：单进程内追问看着一切正常（remember_session 是
    #   `_sessions[tid] = ...` 这种 mutate，改的是真家伙），但**网关一重启
    #   所有续接就静默失效**，之后每次追问都重开会话并回填整段历史 ——
    #   恰好把直连该省的 token 又花回去了，而且不报任何错。
    global _sessions_loaded, _sessions
    if _sessions_loaded:
        return
    _sessions = _load_json(SESSION_FILE)
    _sessions_loaded = True


def _save_sessions() -> None:
    _save_json(SESSION_FILE, _sessions)


def session_for(tid: str) -> dict:
    _load_sessions()
    return dict(_sessions.get(tid) or {})


def remember_session(tid: str, session_id: str, parent: Any,
                     title: str = "") -> None:
    if not tid:
        return
    _load_sessions()
    cur = _sessions.get(tid) or {}
    cur.update({"session_id": session_id, "parent": parent,
                "ts": time.time()})
    if title:
        cur["title"] = cur.get("title") or title
    _sessions[tid] = cur
    # 别让它无限涨
    if len(_sessions) > 60:
        for k in sorted(_sessions, key=lambda x: _sessions[x].get("ts", 0))[:-60]:
            _sessions.pop(k, None)
    _save_sessions()


def forget_session(tid: str) -> None:
    _load_sessions()
    if tid in _sessions:
        _sessions.pop(tid, None)
        _save_sessions()


def enabled(conf: dict) -> bool:
    """是否启用 HTTP 优先。全局开关 + 单站开关，两者都同意才走。

    全局： runtime.http_first（默认 true）—— 一键关掉所有直连
    单站： providers.<id>.http.enabled（默认 true）
    """
    if not bool(settings.get().get("runtime", {}).get("http_first", True)):
        return False
    http = conf.get("http")
    if http is None:
        return True
    if isinstance(http, dict):
        return bool(http.get("enabled", True))
    return bool(http)


async def ask(prompt: str, *, thread: str = "", reset: bool = False,
              thinking: bool = True, search: bool = True,
              timeout: float = 120.0, context: str = "",
              should_stop: Callable[[], bool] | None = None) -> dict:
    """走纯 HTTP 问一次。抛 HttpAuthError / HttpError 时调用方退回浏览器。"""
    cred = load_credentials()
    if not cred.get("cookie"):
        raise HttpAuthError("没有可用凭证（还没收过或已作废）")

    # 会话：同一话题尽量接着上次的会话，省掉重发历史
    session_id = ""
    parent: Any = None
    # resumed 必须记的是"**这一轮真的接上了**上次那个会话吗"，
    # 而不能写成"这个话题在库里有没有会话记录"—— 后者在 reset=True
    # （重试路径就是这么调的）时同样为真，于是刚新建的空会话会被误判成
    # "已接上文"、**跳过历史回填**，重试就等于在一个空白会话里又问了一遍，
    # 上下文全丢。踩过一次：自愈重试的答案反而更差，就是因为这个。
    resumed = False
    if not reset and thread:
        prev = session_for(thread)
        session_id = prev.get("session_id") or ""
        parent = prev.get("parent")
        resumed = bool(session_id)

    t0 = time.time()
    try:
        if not session_id:
            session_id = await asyncio.to_thread(create_session, cred)
            print(f"[http] 新建会话 {session_id[:12]}…", flush=True)
        # 新会话没有上文 → 把历史补进去（和浏览器路线"会话丢了就回填"同一个道理）。
        # 接上了就**不重发** —— 这正是 HTTP 路线省 token 的地方。
        final = prompt if resumed else (
            (context + prompt) if context else prompt)
        if context and not resumed:
            print(f"[http] 新会话，回填 {len(context)} 字节历史", flush=True)
        out = await asyncio.to_thread(
            _stream_completion, cred, session_id=session_id, parent_id=parent,
            prompt=final, thinking=thinking, search=search, timeout=timeout,
            should_stop=should_stop)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        if e.code in (401, 403):
            invalidate(f"HTTP {e.code}")
            raise HttpAuthError(f"HTTP {e.code}：{body}") from e
        raise HttpError(f"HTTP {e.code}：{body}") from e
    except pow_deepseek.PowUnavailable as e:
        raise HttpError(f"PoW 不可用：{e}") from e
    except urllib.error.URLError as e:
        raise HttpError(f"网络错误：{e}") from e

    if thread:
        remember_session(thread, session_id, out.get("message_id"),
                         title=prompt[:24])
    return {
        "answer": out["answer"],
        "thinking": out["thinking"],
        "session_id": session_id,
        "message_id": out.get("message_id"),
        "resumed": resumed,
        "elapsed": time.time() - t0,
    }


def status() -> dict:
    cred = load_credentials()
    ok, why = pow_deepseek.available()
    return {
        "enabled_globally": bool(
            settings.get().get("runtime", {}).get("http_first", True)),
        "credentials": bool(cred.get("cookie")),
        "has_bearer": bool(cred.get("bearer")),
        "saved_at": cred.get("saved_at"),
        "pow_ready": ok,
        "pow_reason": why,
        "sessions": len(_load_json(SESSION_FILE)),
    }
