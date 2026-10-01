"""Token Saver HTTP 网关。

对外暴露的 API 就是给 Agent 用的省钱接口；同时托管控制台页面。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
import webbrowser
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import adapters as pkg
from core import browser, fanout as fanout_mod, killswitch, pool, router, settings, usage

WEB_DIR = settings.ROOT / "web"

# pid -> {"state": "waiting"|"ok"|"timeout", "since": ts}
_LOGIN_SESSIONS: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 空闲巡检：没人再用的站点窗口自动关掉（用户不用再手动关窗口）。
    idle_task = asyncio.create_task(_idle_watchdog())
    try:
        yield
    finally:
        idle_task.cancel()
    await browser.manager.close_all()


async def _idle_watchdog() -> None:
    """空闲巡检：把没人再用的站点窗口自动关掉。

    为什么要有它：以前每次用完都得手动关窗口 —— 用户的原话是"每次调用完还需要
    我手动关闭网页"。这里按 `runtime.idle_close_seconds` 巡检，闲置超时就收掉。
    "还要接着追问"由它天然满足：只要下一问发生在窗口内，计时就一直在续，
    窗口不会关（而且那正是最省 token 的"复用网页会话"路线）。

    每 10 秒看一眼就够 —— 它只是兜底；想立刻关用 POST /api/close
    （MCP: close_windows）。异常一律吞掉重来：巡检把自己搞死，比晚关一个
    窗口严重得多。
    """
    while True:
        await asyncio.sleep(10)
        try:
            secs = float(settings.get()["runtime"].get("idle_close_seconds") or 0)
            if secs > 0:
                await browser.manager.close_idle(secs)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[idle] 巡检异常（已忽略，下轮继续）：{type(e).__name__}: {e}",
                  flush=True)


app = FastAPI(title="Token Saver", version="0.1.0", lifespan=lifespan)

# ---------------------------------------------------------------- 访问控制
# ★ 刻意**不挂 CORS 中间件**（2026-10-01 删掉 allow_origins=["*"]）。
#
# 原来那行 `CORSMiddleware(allow_origins=["*"])` 加上"零鉴权 + 只监听
# 127.0.0.1"是个危险的组合：网关能驱动用户**已登录的网页账号**，
# 而 CORS 通配意味着**用户随便打开的一个恶意网页**都能：
#   · 预检通过 → 发 JSON POST /api/ask → 读回响应
#   于是可以借用户的账号刷免费额度、把回答内容外带出去。
# 控制台页面本来就是同源（都是 127.0.0.1:8787），MCP 桥走 httpx（不走浏览器、
# 不受 CORS 约束），所以删掉它对正常用法零影响。
#
# 再补一道 Host 校验挡住 DNS rebinding（恶意域名解析到 127.0.0.1）：
# 只认回环地址与本机配置的 host。要关掉它设 server.strict_host: false。
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


@app.middleware("http")
async def _host_guard(request: Request, call_next):
    if not settings.get()["server"].get("strict_host", True):
        return await call_next(request)
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    bound = str(settings.get()["server"].get("host") or "").strip("[]")
    if host and host not in _ALLOWED_HOSTS and host != bound:
        return JSONResponse(
            status_code=403,
            content={"ok": False,
                     "error": f"拒绝该 Host（{host}）：只接受本机回环访问。"
                              "需要放宽请设 server.strict_host: false"},
        )
    return await call_next(request)


def _site_lock(pid: str | None):
    """取某站点的 per-site 锁（`asyncio.Lock`，async with 用）。

    **为什么排障类端点也要这把锁**：一个站点只有一个页面
    （`browser.manager._ctx[pid]`），而提问路径（`pool._try_one`）正是靠
    `browser.manager.lock(a.id)` 把同一家的多次调用排成一队。排障 / 抓图 /
    重启这几个端点当初直接调 `ensure_page` / `close`，**没进锁** ——
    "一边跑着 fanout、一边点控制台抓图"时两个协程会同时操作同一个 page：
    轻则抓到别人那一轮的答案，重则一边正在重建上下文、把另一边的页对象
    换成死句柄，报出的错完全看不出是并发引起的。
    """
    return browser.manager.lock(pid or "_")


# ---------------------------------------------------------------- 模型
class AskReq(BaseModel):
    prompt: str
    provider: str | None = None         # 指定则不用自动路由
    system: str | None = None
    thread: str | None = None           # 同一 thread = 同一网页会话，可连续追问
    reset: bool | None = None           # 是否先开新会话（thread 首次调用默认新开）
    timeout: float | None = None
    fallback: bool = True               # 失败是否自动换下一家
    no_mode: bool = False               # 跳过深度思考/模型切换（生图时必须）
    grab_images: bool = False           # 生图任务：等图渲染出来并下载到本地


class AnalyzeReq(BaseModel):
    prompt: str


class FanoutReq(BaseModel):
    """并行外包：把 parts 拆给多家站点同时做。"""
    parts: list[str] = Field(default_factory=list)
    # 模板，支持 {part} / {n} / {total} 占位符。例如：
    #   "把下面这段技术文档翻译成中文，保持术语一致：\n\n{part}"
    # 忘了写 {part} 也不会丢内容 —— 会自动追加在末尾（见 fanout._build_prompt）。
    template: str = ""
    providers: list[str] | None = None   # 指定车道；默认按路由自动挑
    timeout: float | None = None
    max_lanes: int | None = None         # 最多开几条车道（默认 4）
    merge: str = "sections"              # sections | concat | json
    label: str = "第{n}部分"
    # dynamic（默认）= 谁先空谁领下一个，快的车道自然多领，墙钟最短；
    # roundrobin    = 开跑前平均分好，刻意均摊到各家（避免集中消耗某家额度）。
    schedule: str = "dynamic"
    retry_failed: bool = True            # 失败的部分顺序补跑一次


# ---------------------------------------------------------------- 页面
@app.get("/", include_in_schema=False)
async def index():
    # 控制台经常改动，禁用缓存，否则用户会一直跑旧版页面
    return FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/health")
async def health():
    return {"ok": True, "ts": time.time(), "version": "0.1.0"}


# ---------------------------------------------------------------- 元信息
@app.get("/api/meta")
async def meta():
    cfg = settings.get()
    return {
        "browser": cfg["browser"],
        "runtime": cfg["runtime"],
        "pricing": cfg["pricing"],
        "running": browser.manager.running(),
    }


@app.get("/api/providers")
async def api_providers():
    return await pool.provider_states()


@app.post("/api/providers/{pid}/toggle")
async def toggle(pid: str, payload: dict):
    """在 config.yaml 里开关某个 provider。"""
    enabled = bool(payload.get("enabled", True))
    _write_provider_conf(pid, {"enabled": enabled})
    if not enabled:
        await browser.manager.close(pid)
    return {"ok": True, "id": pid, "enabled": enabled}


def _write_provider_conf(pid: str, patch: dict) -> None:
    """程序侧的配置改动写进 overrides.yaml，**不动 config.yaml**。

    config.yaml 是手写文档、带注释；用 yaml.dump 整份回写会把注释冲光。
    """
    import yaml
    f = settings.OVERRIDES_FILE
    current: dict[str, Any] = {}
    if f.exists():
        try:
            current = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            current = {}
    if not isinstance(current, dict):
        current = {}
    prov = current.setdefault("providers", {})
    if not isinstance(prov, dict):
        prov = current["providers"] = {}
    prov.setdefault(pid, {}).update(patch)
    f.write_text(
        "# 本文件由 Token Saver 自动生成，记录你在控制台里的改动。\n"
        "# 手写配置请写 config.yaml（那边不会被程序覆盖）。\n"
        + yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    settings.reload()


# ---------------------------------------------------------------- 登录
@app.post("/api/providers/{pid}/login")
async def login(pid: str):
    a = pkg.get(pid)
    if not a:
        raise HTTPException(404, f"unknown provider {pid}")
    try:
        async with _site_lock(pid):
            msg = await browser.manager.open_login(a, headless=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    _LOGIN_SESSIONS[pid] = {"state": "waiting", "since": time.time()}
    # 后台轮询登录态
    asyncio.create_task(_watch_login(a))
    return {"ok": True, "message": msg, "id": pid}


async def _watch_login(a, budget: float = 600.0) -> None:
    deadline = time.time() + budget
    while time.time() < deadline:
        sess = _LOGIN_SESSIONS.get(a.id)
        if not sess or sess["state"] != "waiting":
            return
        await asyncio.sleep(2.0)
        try:
            # 同样要持锁：轮询期间用户可能正在提问，两边都不该同时重建上下文
            async with _site_lock(a.id):
                page = await browser.manager.ensure_page(a)
                logged = await a.is_logged_in(page)
            if logged:
                _LOGIN_SESSIONS[a.id] = {"state": "ok", "since": time.time()}
                return
        except Exception:
            await asyncio.sleep(1.0)
    if _LOGIN_SESSIONS.get(a.id, {}).get("state") == "waiting":
        _LOGIN_SESSIONS[a.id] = {"state": "timeout", "since": time.time()}


@app.get("/api/providers/{pid}/login-status")
async def login_status(pid: str):
    a = pkg.get(pid)
    if not a:
        raise HTTPException(404, "unknown provider")
    sess = _LOGIN_SESSIONS.get(pid, {"state": "idle", "since": 0})
    waiting = round(time.time() - sess.get("since", time.time()), 1)
    return {
        "id": pid,
        "state": sess.get("state", "idle"),
        "waiting": waiting if sess.get("state") == "waiting" else 0,
    }


@app.post("/api/stop")
async def stop_all(payload: dict | None = None):
    """全局停机：立刻叫停正在进行和后续的所有调用。

    批量外包时想中止就用它 —— 关掉浏览器是不够的，自愈逻辑会把它重新打开。
    停机后浏览器**不会被再启动**，返回的原因里写明了怎么恢复。
    """
    reason = (payload or {}).get("reason") or "通过 /api/stop 手动停机"
    killswitch.halt(reason)
    # 顺手把已经开着的窗口关掉，视觉上也停下来
    try:
        await browser.manager.close_all()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "halted": True, "reason": killswitch.switch.halted()}


@app.post("/api/resume")
async def resume_all():
    """解除停机。会删掉 data/STOP 哨兵文件并清空重建计数。"""
    killswitch.resume()
    return {"ok": True, "halted": False, "status": killswitch.switch.status()}


@app.post("/api/shutdown")
async def shutdown(payload: dict | None = None):
    """优雅退出：先关掉所有浏览器窗口，再结束进程。

    专门给"改完代码要重启加载"用的。以前只能硬杀 python 进程，
    结果是 Playwright 拉起来的 Edge 变成孤儿进程继续占着
    data/profiles/<站点> 的 profile 锁 —— 下次启动直接报"profile 被占用"，
    而且因为只有一个内核的错误被显示，还容易被误读成"没装浏览器"。
    走这里则先把上下文关干净，再让进程自己退出，不留尾巴。

    exit_code=0 表示正常重启（配合后台启动.vbs 或启动服务.bat 再拉起来）；
    传 {"code": 1} 则用于脚本里判断"是重启还是异常退出"。
    """
    code = int((payload or {}).get("code") or 0)
    try:
        await browser.manager.close_all()
    except Exception:  # noqa: BLE001
        pass
    # 让响应先发出去，再退出 —— 直接 os._exit 会让 curl 看到空的回复
    asyncio.get_running_loop().call_later(0.3, os._exit, code)
    return {"ok": True, "shutting_down": True, "exit_code": code}


@app.get("/api/status")
async def status():
    return {
        "halted": bool(killswitch.switch.halted()),
        "reason": killswitch.switch.halted(),
        "stop_file": str(killswitch.STOP_FILE),
        "running": browser.manager.running(),
        # 空闲自动关闭：当前闲置秒数、以及最近自动关过谁（排障用）
        "idle_close_seconds": settings.get()["runtime"].get("idle_close_seconds"),
        **browser.manager.idle_report(),
    }


@app.get("/api/http")
async def http_status():
    """纯 HTTP 直连的状态：凭证有没有、PoW 能不能跑、有几个直连会话。

    想知道"为什么还在开浏览器"就看这里 —— pow_ready=false 或
    credentials=false 都意味着直连走不通、只能退回浏览器。
    """
    from core import http_deepseek
    return http_deepseek.status()


@app.post("/api/close")
async def close_windows(payload: dict | None = None):
    """**只关浏览器窗口，不停服务**（区别于 /api/shutdown 与 /api/stop）。

    不给 provider 就全关。每个站点都走 per-site 锁：正在提问的那一站会
    等它问完再关（在锁外关页面会把协程手上的 page 抽走，报随机 TargetClosed）。
    登录态不受影响 —— cookie 在持久化 profile 里，下次调用自动重开并回填。
    """
    req = payload or {}
    pid = (req.get("provider") or req.get("id") or "").strip()
    running = browser.manager.running()
    targets = [pid] if pid else list(running)
    closed: list[str] = []
    for x in targets:
        if x not in running:
            continue
        async with _site_lock(x):
            await browser.manager.close(x)
        closed.append(x)
    return {"ok": True, "closed": closed, "running": browser.manager.running()}


@app.post("/api/providers/{pid}/stop")
async def stop(pid: str):
    _LOGIN_SESSIONS.pop(pid, None)
    # close() 会动 _ctx/_page。不持锁的话，正好撞上该站点在跑的一轮提问时，
    # 会把提问手上的 page 抽走 —— 那边看到的是随机的 TargetClosed。
    async with _site_lock(pid):
        await browser.manager.close(pid)
    return {"ok": True, "id": pid}


@app.post("/api/providers/{pid}/restart")
async def restart(pid: str):
    """重置这个站点的浏览器句柄。窗口被关掉/报 TargetClosed 之后用它恢复，
    登录态不会丢（存在持久化 profile 里）。"""
    _LOGIN_SESSIONS.pop(pid, None)
    async with _site_lock(pid):
        await browser.manager.close(pid)
    return {"ok": True, "id": pid,
            "message": "浏览器已重置，下次调用会重新打开窗口（登录态保留）"}


# ---------------------------------------------------------------- 提问
@app.post("/api/ask")
async def api_ask(req: AskReq):
    if not req.prompt.strip():
        raise HTTPException(400, "prompt 不能为空")
    final = req.prompt
    if req.system:
        final = f"{req.system}\n\n---\n\n{req.prompt}"
    try:
        res = await pool.ask(final, provider=req.provider, thread=req.thread,
                             reset=req.reset, timeout=req.timeout,
                             fallback=req.fallback, no_mode=req.no_mode,
                             grab_images=req.grab_images)
    except Exception:  # noqa: BLE001
        # 注意参数顺序：JSONResponse 第一个参数是 content，status_code 要用关键字。
        # 写成 JSONResponse(500, {...}) 会在出错时再抛一个 TypeError，
        # 结果调用方只能看到一个空的 500。
        return JSONResponse({"ok": False, "error": traceback.format_exc(limit=3)},
                            status_code=500)
    return res


@app.post("/api/fanout")
async def api_fanout(req: FanoutReq):
    """并行外包：同一个任务的不同部分，同时丢给多家站点做。

    适合"拆开就没有依赖"的活：长文档分块翻译/摘要、批量改写、
    多份素材各自提炼要点……不适合需要带前文的连环追问（那用 /api/ask + thread）。
    """
    if not req.parts:
        raise HTTPException(400, "parts 不能为空")
    try:
        return await fanout_mod.fanout(
            req.parts, template=req.template, providers=req.providers,
            timeout=req.timeout, merge=req.merge, label=req.label,
            max_lanes=req.max_lanes, retry_failed=req.retry_failed)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": traceback.format_exc(limit=3)},
                            status_code=500)


@app.post("/api/debug/dom")
async def debug_dom(payload: dict):
    """排障用：dump 站点页面上可疑的可点击元素，用于修正选择器。

    真实现在 `_debug_dom`；外面这层只负责先取该站点的 per-site 锁
    （理由见 `_site_lock`：不持锁会和正在跑的提问抢同一个 page）。
    """
    async with _site_lock(payload.get("provider")):
        return await _debug_dom(payload)


async def _debug_dom(payload: dict):
    pid = payload.get("provider")
    a = pkg.get(pid)
    if not a:
        raise HTTPException(404, "unknown provider")
    try:
        page = await browser.manager.ensure_page(a)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    kind = payload.get("kind", "clickable")
    # 直接看 snapshot_answer() 抓到什么 —— 排查"会话是否还在"的判断失准时用
    if kind == "snapshot":
        txt = ""
        try:
            txt = await a.snapshot_answer(page)
        except Exception as e:  # noqa: BLE001
            txt = f"<抛错 {type(e).__name__}: {e}>"
        return {"provider": pid, "url": page.url, "kind": kind,
                "len": len(txt), "head": txt[:400], "tail": txt[-300:],
                "pick": getattr(a, "answer_pick", None)}
    # 直接吐某个选择器的 outerHTML —— 摸清一个复杂组件内部结构时最省事
    if kind == "sel":
        sel = payload.get("selector") or "body"
        try:
            html = await page.evaluate(
                """(sel) => {
                    const e = document.querySelector(sel);
                    return e ? e.outerHTML.replace(/\\s+/g, ' ') : '(not found)';
                }""", sel)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"{type(e).__name__}: {e}")
        lim = int(payload.get("limit") or 4000)
        return {"provider": pid, "url": page.url, "kind": kind,
                "selector": sel, "len": len(html), "html": html[:lim]}
    # 排障用：在**生成过程中**反复调它，一次拿到"答案候选 + 停止按钮 + 流式指示"
    #
    # 为什么单列一个 kind：定位"答案被腰斩"时，需要把"文本长度随时间怎么变"
    # 和"当时页面上有没有停止按钮"对上时间轴。分开调 answer_probe 和
    # clickable 会各多一次往返，采样点对不齐，还容易被别的东西插队。
    if kind == "watch":
        data = await page.evaluate(
            """(sels) => {
              const cands = [];
              for (const sel of sels) {
                let els = [];
                try { els = Array.from(document.querySelectorAll(sel)); }
                catch (e) { continue; }
                els.forEach((e, i) => {
                  const r = e.getBoundingClientRect();
                  cands.push({
                    sel: sel, i: i,
                    cls: (e.className || '').toString().slice(0, 60),
                    nested: els.some((o, j) => j !== i && o.contains(e)),
                    visible: !!(r.width && r.height),
                    len: (e.innerText || '').trim().length,
                    head: (e.innerText || '').trim().slice(0, 40).replace(/\\s+/g, ' ')
                  });
                });
              }
              // 停止/中止类按钮：文字、aria-label、class 任一命中
              const btns = [];
              document.querySelectorAll('button, [role="button"], [class*="btn" i]')
                .forEach(b => {
                  const r = b.getBoundingClientRect();
                  if (!r.width || !r.height) return;
                  const txt = (b.innerText || '').trim();
                  const aria = (b.getAttribute('aria-label') || '');
                  const cls = (b.className || '').toString();
                  const title = (b.getAttribute('title') || '');
                  const hay = (txt + ' ' + aria + ' ' + title).toLowerCase();
                  if (hay.includes('停止') || hay.includes('stop') || hay.includes('中止')
                      || hay.includes('暂停') || /stop|abort|interrupt/i.test(cls)) {
                    btns.push({text: txt.slice(0, 12), aria: aria.slice(0, 30),
                               title: title.slice(0, 20), cls: cls.slice(0, 60)});
                  }
                });
              const flags = {};
              for (const sel of ['[class*="streaming" i]', '[class*="typing" i]',
                                 '[class*="thinking" i]', '[class*="generating" i]',
                                 '.hyc-component-deepsearch-cot']) {
                try {
                  const e = document.querySelector(sel);
                  flags[sel] = e ? (e.innerText || '').trim().length : -1;
                } catch (_) { flags[sel] = null; }
              }
              return {cands: cands, stop_buttons: btns, flags: flags};
            }""",
            a._merge("answer", payload.get("sels") or ["[class*='markdown']"]))
        return {"provider": pid, "url": page.url, "kind": kind, **data}
    try:
        if kind == "text":
            # 按可见文字找元素，并返回它最近的可点击祖先（用来定位开关按钮）
            word = payload.get("word") or "思考"
            data = await page.evaluate(
                """(word) => {
                  const hits = Array.from(document.querySelectorAll('*'))
                    .filter(e => (e.textContent || '').trim().includes(word))
                    .filter(e => { const r = e.getBoundingClientRect(); return e.children.length === 0 && r.width > 0; })
                    .slice(0, 12);
                  return hits.map(e => {
                    let p = e, guard = 0;
                    while (p && guard++ < 6) {
                      const role = p.getAttribute && p.getAttribute('role');
                      if (role === 'button' || p.tagName === 'BUTTON') break;
                      p = p.parentElement;
                    }
                    const chain = [];
                    let q = e, g2 = 0;
                    while (q && g2++ < 5) {
                      chain.push({
                        tag: q.tagName,
                        cls: (q.className || '').toString().slice(0, 70),
                        role: q.getAttribute && (q.getAttribute('role') || ''),
                        pressed: q.getAttribute && (q.getAttribute('aria-pressed') || ''),
                        dataState: q.getAttribute && (q.getAttribute('data-state') || ''),
                      });
                      q = q.parentElement;
                    }
                    return {
                      text: (e.textContent || '').trim().slice(0, 30),
                      tag: e.tagName,
                      cls: (e.className || '').toString().slice(0, 60),
                      clickable: !!p,
                      chain: chain,
                      clickHtml: p ? p.outerHTML.replace(/\\s+/g, ' ').slice(0, 300) : ''
                    };
                  });
                }""", word)
        elif kind == "raw":
            data = await page.evaluate(
                """() => Array.from(document.querySelectorAll(
                     'button, [role="button"], [class*="ds-button"], [class*="copy" i]'))
                   .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                   .slice(-8).map(e => e.outerHTML.replace(/\\s+/g,' ').slice(0,320))""")
        elif kind == "answers":
            data = await page.evaluate(
                """() => Array.from(document.querySelectorAll(
                     '[class*="markdown" i], [class*="message" i], article, [class*="answer" i]'))
                   .slice(-6).map(e => ({
                     cls: (e.className||'').toString().slice(0,80),
                     tag: e.tagName,
                     text: (e.innerText||'').slice(0,60)
                   }))""")
        elif kind == "answer_probe":
            # 排障用：snapshot_answer() 到底从哪些元素里挑答案？
            #
            # 泛选择器（如元宝的 div[class*='markdown']）会同时命中**外层容器
            # 和它的每一个子块**。如果抽取器直接从"最后一个命中"往前找，
            # 拿到的往往只是容器里的最后一个小块 —— 答案被腰斩，而且不报错。
            # 实测元宝：一份含表格的回答只捞回了最后那张表（158 字）。
            #
            # 这里把每个候选选择器的命中列表、各自文本长度、以及
            # "是否嵌套在别的命中元素内部"全列出来，一眼就能看出该挑哪个。
            from core.adapter import GENERIC_ANSWER_SELECTORS
            sels = a._merge("answer", GENERIC_ANSWER_SELECTORS)
            data = await page.evaluate(
                """(sels) => sels.map(sel => {
                     let els = [];
                     try { els = Array.from(document.querySelectorAll(sel)); }
                     catch (e) { return {sel: sel, error: String(e)}; }
                     return {
                       sel: sel,
                       count: els.length,
                       items: els.map((e, i) => {
                         const r = e.getBoundingClientRect();
                         const t = (e.innerText || '').trim();
                         return {
                           i: i,
                           cls: (e.className || '').toString().slice(0, 55),
                           nested: els.some((o, j) => j !== i && o.contains(e)),
                           visible: !!(r.width && r.height),
                           len: t.length,
                           head: t.slice(0, 45).replace(/\\s+/g, ' ')
                         };
                       })
                     };
                   })""", sels)
        elif kind == "popups":
            # 排障用：页面上现在到底有什么浮层？它们压住了输入框没有？
            # 命中 listed 却一个 close 候选都没有 → 说明这套启发式对该弹窗失效，
            # 需要手工补 providers.<id>.selectors.popup_close。
            data = await page.evaluate(
                """(roots) => {
                  const out = [];
                  const seen = new Set();
                  for (const rs of roots) {
                    document.querySelectorAll(rs).forEach(e => {
                      if (seen.has(e)) return;
                      seen.add(e);
                      const r = e.getBoundingClientRect();
                      const cs = getComputedStyle(e);
                      if (cs.visibility === 'hidden' || cs.display === 'none') return;
                      if (!r.width || !r.height) return;
                      const cl = [];
                      e.querySelectorAll('button, [role="button"], a').forEach(b => {
                        const br = b.getBoundingClientRect();
                        if (!br.width || !br.height) return;
                        cl.push({
                          tag: b.tagName,
                          text: (b.innerText || '').trim().slice(0, 16),
                          aria: b.getAttribute('aria-label') || '',
                          cls: (b.className || '').toString().slice(0, 60)
                        });
                      });
                      out.push({
                        sel: rs,
                        tag: e.tagName,
                        cls: (e.className || '').toString().slice(0, 80),
                        area: Math.round(r.width * r.height),
                        rect: [Math.round(r.x), Math.round(r.y),
                               Math.round(r.width), Math.round(r.height)],
                        zIndex: cs.zIndex,
                        text: (e.innerText || '').trim().slice(0, 120),
                        buttons: cl.slice(0, 12)
                      });
                    });
                  }
                  return out;
                }""", payload.get("roots") or [
                    "[role='dialog']", "[aria-modal='true']",
                    "[class*='modal' i]", "[class*='dialog' i]",
                    "[class*='popup' i]", "[class*='drawer' i]",
                    "[class*='overlay' i]", "[class*='update-log' i]",
                    "[class*='announcement' i]",
                ])
        elif kind == "toggles":
            # 扫页面上所有"像开关"的元素：文字命中模式关键词 + 带状态属性
            WORDS = payload.get("words") or [
                "思考", "推理", "深度", "联网", "搜索", "联网搜索",
                "reasoning", "think", "Think", "search", "Search", "web",
            ]
            data = await page.evaluate(
                """(words) => {
                  const STATE = ['aria-pressed','aria-checked','aria-selected',
                                 'data-state','data-active','data-checked',
                                 'data-enabled','data-on'];
                  const out = [];
                  const els = Array.from(document.querySelectorAll(
                    'button, [role="button"], [role="switch"], [role="checkbox"], label, div, span'));
                  for (const e of els) {
                    const _r = e.getBoundingClientRect();
                    if (!_r.width || !_r.height) continue;
                    const txt = (e.textContent || '').trim();
                    if (txt.length > 24) continue;              // 太长的多半是容器
                    if (!words.some(w => txt.includes(w))) continue;
                    const r = e.getBoundingClientRect();
                    if (!r.width || !r.height) continue;
                    if (r.width * r.height > 160000) continue;  // 排除整块工具栏
                    const st = {};
                    for (const a of STATE) {
                      const v = e.getAttribute(a);
                      if (v !== null && v !== undefined && v !== '') st[a] = v;
                    }
                    out.push({
                      tag: e.tagName,
                      text: txt.slice(0, 24),
                      cls: (e.className || '').toString().slice(0, 70),
                      state: st,
                      area: Math.round(r.width * r.height)
                    });
                  }
                  // 按面积升序，最小的最可能是按钮本体
                  out.sort((a, b) => a.area - b.area);
                  return out.slice(0, 40);
                }""", WORDS)
        else:
            data = await page.evaluate(
                """() => Array.from(document.querySelectorAll(
                     'button, [role="button"], [class*="copy" i], [class*="btn" i]'))
                   .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                   .slice(-30).map(e => ({
                     tag: e.tagName,
                     cls: (e.className||'').toString().slice(0,70),
                     aria: e.getAttribute('aria-label') || '',
                     title: e.getAttribute('title') || '',
                     text: (e.innerText||'').trim().slice(0,20)
                   }))""")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    if kind == "raw":
        return {"provider": pid, "url": page.url, "kind": kind,
                "items": [{"html": h} for h in data]}
    if kind == "text":
        return {"provider": pid, "url": page.url, "kind": kind, "items": data}
    return {"provider": pid, "url": page.url, "kind": kind, "items": data}


@app.post("/api/images")
async def api_images(payload: dict):
    """抓取当前页面上"像生成结果"的图片并下载到本地。

    给豆包/元宝这类**能生图**的站点用：图片在 DOM 里是 <img>，
    但页面内 fetch 会被图床的 CORS 挡下，所以走 Playwright 的网络栈下载。
    返回本地路径，直接就能拿去展示。

    这个端点会**等图渲染出来**（wait_sec 最多 180 秒），等待期间一直占着
    该站点的 per-site 锁 —— 期间对同一站点的提问会排队。这是有意的：
    图和那一轮对话在同一个 page 上，放开并发只会互相踩。
    """
    async with _site_lock(payload.get("provider")):
        return await _images_impl(payload)


async def _images_impl(payload: dict):
    pid = payload.get("provider")
    a = pkg.get(pid)
    if not a:
        raise HTTPException(404, "unknown provider")
    try:
        page = await browser.manager.ensure_page(a)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    min_side = int(payload.get("min_side") or 512)
    # wait_sec：服务端轮询等图片渲染出来。生图是异步的 —— 文字回复出来时
    # 图往往还在画（实测差 10~40s），让调用方反复来问很不友好。
    wait_sec = float(payload.get("wait_sec") or 0)
    deadline = time.time() + min(wait_sec, 180.0)
    urls = await a.extract_images(page, min_side=min_side)
    while not urls and time.time() < deadline:
        await asyncio.sleep(2.5)
        urls = await a.extract_images(page, min_side=min_side)
    if not urls:
        return {"provider": pid, "count": 0, "files": [], "urls": [],
                "url": page.url,
                "note": "页面上没有够大的生成图片；若刚发完提示词，"
                        "可加大 wait_sec 或稍后重试。"
                        "若 url 是站点首页，说明浏览器上下文被重建了、"
                        "当前会话已丢，图也随之消失"}
    files = await a.download_images(
        page, urls, referer=(a.homepage or a.url or ""))
    return {"provider": pid, "count": len(files),
            "files": files, "urls": urls}


@app.post("/api/debug/dismiss-popup")
async def debug_dismiss(payload: dict):
    """排障用：手动清一次浮层，并回报清理前后页面上还剩什么。

    弹窗挡路时先跑 /api/debug/dom {"kind":"popups"} 看它长什么样，
    再跑这个验证能不能关掉。关不掉的话，把列出的容器 class 填进
    providers.<id>.selectors.popup_close 即可。
    """
    async with _site_lock(payload.get("provider")):
        return await _dismiss_impl(payload)


async def _dismiss_impl(payload: dict):
    pid = payload.get("provider")
    a = pkg.get(pid)
    if not a:
        raise HTTPException(404, "unknown provider")
    try:
        page = await browser.manager.ensure_page(a)
        before = await a._has_popup(page)
        closed = await a.dismiss_popups(page, rounds=int(payload.get("rounds", 2)))
        after = await a._has_popup(page)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    # 还留着的东西列出来，方便决定要不要补自定义选择器
    remain = []
    if after:
        try:
            remain = await page.evaluate(
                """(roots) => {
                  const out = [];
                  for (const rs of roots) {
                    const e = document.querySelector(rs);
                    if (!e) continue;
                    const r = e.getBoundingClientRect();
                    if (!r.width || !r.height) continue;
                    if (r.width * r.height < 40000) continue;
                    out.push({
                      sel: rs,
                      cls: (e.className || '').toString().slice(0, 80),
                      area: Math.round(r.width * r.height),
                      text: (e.innerText || '').trim().slice(0, 100)
                    });
                  }
                  return out;
                }""", ["[role='dialog']", "[aria-modal='true']",
                       "[class*='modal' i]", "[class*='dialog' i]",
                       "[class*='popup' i]", "[class*='overlay' i]"])
        except Exception:
            pass
    return {"provider": pid, "had_popup": before, "still_open": after,
            "closed": closed, "remaining": remain}


@app.post("/api/debug/click")
async def debug_click(payload: dict):
    """排障用：点一下页面上含某文字的最小可见元素，返回点击后出现的新元素。

    用来摸清"点开关/点下拉"之后菜单长什么样，好把 mode_pick 写对。
    """
    async with _site_lock(payload.get("provider")):
        return await _click_impl(payload)


async def _click_impl(payload: dict):
    pid = payload.get("provider")
    a = pkg.get(pid)
    if not a:
        raise HTTPException(404, "unknown provider")
    word = payload.get("word") or ""
    if not word:
        raise HTTPException(400, "word required")
    try:
        page = await browser.manager.ensure_page(a)
        loc = await a._find_toggle(page, word)
        if loc is None:
            return {"provider": pid, "clicked": False, "reason": "element not found"}
        before = await page.evaluate(
            """() => Array.from(document.querySelectorAll('*'))
                 .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; }).length""")
        await loc.click(timeout=4000)
        await page.wait_for_timeout(900)
        items = await page.evaluate(
            """() => {
              const STATE = ['aria-checked','aria-selected','aria-pressed',
                             'data-state','data-active','data-selected'];
              return Array.from(document.querySelectorAll(
                       '[role="menuitem"], [role="option"], [role="listitem"], li, button, div'))
                .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                .map(e => {
                  const txt = (e.textContent || '').trim();
                  const st = {};
                  for (const a of STATE) {
                    const v = e.getAttribute(a);
                    if (v !== null && v !== undefined && v !== '') st[a] = v;
                  }
                  return { text: txt.slice(0, 30), tag: e.tagName,
                           cls: (e.className || '').toString().slice(0, 60), state: st };
                })
                .filter(x => x.text && x.text.length <= 30)
                .slice(0, 60);
            }""")
        after = await page.evaluate(
            """() => Array.from(document.querySelectorAll('*'))
                 .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; }).length""")
        return {"provider": pid, "clicked": True, "url": page.url,
                "total_before": before, "total_after": after, "menu": items}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.get("/api/threads")
async def api_threads():
    return pool.list_threads()


@app.post("/api/threads/new")
async def api_thread_new():
    return {"id": pool.new_thread_id()}


@app.post("/api/analyze")
async def analyze(req: AnalyzeReq):
    """先别急着发：看看这个任务值不值得外包。"""
    return router.complexity(req.prompt)


# ---------------------------------------------------------------- 统计
@app.get("/api/stats")
async def stats():
    return await usage.asummary()


@app.post("/api/stats/reset")
async def stats_reset():
    await usage.areset()
    return {"ok": True}


@app.get("/shots/{name}")
async def shot(name: str):
    p = settings.SHOT_DIR / name
    if not p.exists():
        raise HTTPException(404, "no such shot")
    return FileResponse(p)


@app.get("/api/shots")
async def list_shots():
    files = sorted(settings.SHOT_DIR.glob("*.png"),
                   key=lambda x: x.stat().st_mtime, reverse=True)[:12]
    return [{"name": f.name, "url": f"/shots/{f.name}"} for f in files]


@app.get("/api/skill", response_class=PlainTextResponse)
async def skill_snippet():
    """给 Agent 看的调用说明，可直接塞进 system prompt。"""
    base = "http://127.0.0.1:{}/api".format(settings.get()["server"]["port"])
    return f"""# Token Saver 使用说明（廉价算力外包网关）

## 你（Agent）什么时候该主动调它
- 翻译、摘要、润色、改写、格式转换、取标题、正则、单位换算、术语解释
- 任何"丢过去就能出结果、不需要你反复推理"的标准化任务
- **遇到这类任务时应直接调用，不要先问用户**，拿到结果后自行质检再转述
- 多文件改造、架构设计、根因分析、长链路推理不要外包，自己做

## 调用方式
POST {base}/ask
  body: {{"prompt": "...", "provider": "auto"}}
  可选 thread: "话题id" —— 同一话题的追问带上同一个 id，网页端会记得上文；
  不带 thread = 一次性外包，每次都是全新会话。
  返回: {{"ok": true, "answer": "...", "provider": "deepseek", "thread": "..."}}

## 先评估要不要外包（可选）
POST {base}/analyze  body: {{"prompt": "..."}}
  返回 verdict: outsource / borderline / keep_local

## 查看当前可用站点
GET {base}/providers

拿到 answer 后你仍需判断质量，不合适就自己重做，别让用户感知到失败。"""


try:
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
except Exception:
    pass


def main():
    cfg = settings.get()["server"]
    url = f"http://{cfg['host']}:{cfg['port']}"
    print(f"[token-saver] 控制台: {url}")
    print("[token-saver] Agent 接口: " + url + "/api/ask")
    if "--no-open" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    import uvicorn
    uvicorn.run(app, host=cfg["host"], port=int(cfg["port"]), log_level="warning")


if __name__ == "__main__":
    main()
