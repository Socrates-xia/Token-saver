"""编排层：把一次「外包提问」跑完整。

流程：选 provider → 取串行锁 → 确保页面就绪 → adapter.ask → 记用量 → 失败自动换下一家。

会话（thread）机制：
- **点名话题**（调用方给了 thread）：追问落在同一个站点的同一个对话里，
  网页端模型记得上文；网页会话丢了就回填话题历史兜底，对话接得上；
- **不给 thread**：自动挂到该站点的隐式话题 `auto-<站点>` 上。它**只复用
  网页会话**（连着追问仍是同一个会话，不重发上文），但**不记也不回填
  历史**。原因见 `_history_of`：这个键是**按站点共享**的，攒下的内容会
  横跨毫不相干的任务（实测 18 小时前的"列举中国节日"和之后的翻译、生图
  全挤在同一个 `auto-doubao` 里，一开生图窗口就把旧对话整段发了出去）。
  想让它"会话丢了也能接上"，就显式传 thread。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import Counter
from typing import Any

from . import adapters as provider_pkg
from . import browser, router, settings, usage
from .adapter import strip_noise
from .killswitch import resume, resume_hint, switch  # noqa: F401  resume 供上层复用
from .http_deepseek import HttpStopped

THREADS_FILE = settings.DATA_DIR / "threads.json"
_MAX_THREADS = 40
# 回填给网页端的上下文：最多几轮、每轮最多多少字
_CTX_MAX_TURNS = 6
_CTX_MAX_CHARS = 3000
_CTX_MAX_TOTAL = 8000

# ★ 这两段文案有一条硬约束：**不许出现 core/adapter.py 里 _REASONING_MARKERS
#   的词**（"用户问"、"助手答"、"对话："…）。那张表是给「深度思考在复述
#   对话」用的豁免名单，回填块一旦命中它，looks_like_echo 的回显判定就会被
#   关掉 —— 实测后果是回填过的生图请求把用户消息当答案收了，还滚进话题历史
#   （2026-10-01）。tools/selftest_thread.py 里有一条断言把这件事锁死。
CTX_HEADER = ("（下面是一段历史记录，供你接续上下文。如果你在页面上已经能看到"
              "这段内容，就直接忽略它、只回答最后的新问题）\n")
CTX_FOOTER = "\n（历史记录到此为止，下面才是我的新问题）\n"


def _build_context(messages: list[dict]) -> str:
    """把话题历史拼成一段上下文。网页端的会话本身并不可靠
    （服务重启、浏览器重开、站点策略都可能让它丢失），
    所以由服务端来兜住这份记忆。"""
    if not messages:
        return ""
    recent = messages[-_CTX_MAX_TURNS:]
    parts: list[str] = []
    total = 0
    for m in recent:
        q = (m.get("q") or "")[:_CTX_MAX_CHARS]
        a = (m.get("a") or "")[:_CTX_MAX_CHARS]
        block = f"【历史】上轮提问：{q}\n【历史】上轮答复：{a}\n"
        if total + len(block) > _CTX_MAX_TOTAL:
            break
        parts.append(block)
        total += len(block)
    if not parts:
        return ""
    return CTX_HEADER + "\n".join(parts) + CTX_FOOTER


def _is_implicit_thread(tid: str) -> bool:
    """是不是「隐式续聊话题」—— 没给 thread 时按站点自动挂上的 auto-<站点>。"""
    return bool(tid) and tid.startswith("auto-")


def _history_of(tid: str, remembered: dict | None) -> list[dict]:
    """这个话题里**可以拿来回填**的历史。

    ★ 隐式话题一律为空。它只是「同一站点连续追问」的载体，攒下的东西会横跨
    毫不相干的任务 —— 实测 auto-doubao 里既有 18 小时前的「列举中国节日」，
    又有之后的翻译和生图。一旦回填，用户看到的就是「新开的生图窗口，发出去的
    提示词里带着很久前的对话」（2026-10-01 用户实测反馈）。
    会话页指针（chat_url）照旧复用，所以「连着追问」的体验不受影响。
    没有话题（连 auto- 都没有）时同样为空 —— 那种调用按定义就是一次性外包。
    """
    if not tid or not remembered or _is_implicit_thread(tid):
        return []
    return remembered.get("messages") or []


_threads: dict[str, dict] = {}
_loaded = False


def _load_threads() -> None:
    global _loaded
    if _loaded:
        return
    try:
        _threads.update(json.loads(THREADS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    _loaded = True


def _save_threads() -> None:
    try:
        THREADS_FILE.write_text(
            json.dumps(_threads, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def list_threads() -> list[dict]:
    _load_threads()
    out = sorted(_threads.values(), key=lambda t: t.get("ts", 0), reverse=True)
    return out[:_MAX_THREADS]


def get_thread(tid: str) -> dict | None:
    _load_threads()
    return _threads.get(tid)


def _touch_thread(tid: str, pid: str, prompt_head: str,
                 chat_url: str = "", ctx_born: float = 0.0) -> None:
    _load_threads()
    cur = _threads.get(tid, {})
    cur.update({
        "id": tid,
        "provider": pid,
        "title": cur.get("title") or (prompt_head or "未命名话题")[:24],
        "ts": time.time(),
    })
    # 记住这个话题停在网页端的哪个会话页上。下次追问时比对一下：
    # 还是同一个 URL → 网页端自己有上文，直接问就行，不用把历史重发一遍。
    if chat_url:
        cur["chat_url"] = chat_url
    if ctx_born:
        cur["ctx_born"] = ctx_born      # 记住当时用的是哪个浏览器上下文
    cur.setdefault("messages", [])
    _threads[tid] = cur
    if len(_threads) > _MAX_THREADS:
        for k in sorted(_threads, key=lambda k: _threads[k].get("ts", 0))[:-_MAX_THREADS]:
            _threads.pop(k, None)
    _save_threads()


def _append_message(tid: str, question: str, answer: str, pid: str,
                    chat_url: str = "") -> None:
    _load_threads()
    cur = _threads.get(tid)
    if not cur:
        return
    msgs = cur.setdefault("messages", [])
    msgs.append({
        "q": question[:4000],
        "a": answer[:8000],
        "p": pid,
        "ts": time.time(),
    })
    if chat_url:
        cur["chat_url"] = chat_url
    cur["messages"] = msgs[-20:]
    cur["ts"] = time.time()
    _save_threads()


# 模型在"其实没看到上文"时会反问我们已经交代过的信息。实测一次：
# 明明刚问过"永城的天气"，追问"那边有什么好玩的地方"却回"你说的'那边'
# 具体是哪里呀"。命中这些模式就说明这轮上下文没接上。
_MISSING_CTX_RE = re.compile(
    r"具体是(?:指)?哪里|哪个城市|是哪里(?:呀|呢)?|指的?是哪"
    r"|你(?:说的|指的)[^。！？]{0,8}是(?:哪|什么)"
    r"|请(?:先)?(?:告诉我|补充)[^。！？]{0,12}(城市|目的地|地点|地区)"
    r"|需要先了解|先补充(?:一下)?信息|信息还?不够")


def _looks_like_missing_ctx(text: str) -> bool:
    """回答在"反问我们刚给过的信息"吗？→ 判定为上下文没接上。"""
    t = (text or "").strip()
    if not t or len(t) > 600:
        return False
    return bool(_MISSING_CTX_RE.search(t))


# 追问时的指代词 —— 出现这些词才说明"这轮必须依赖上文"
_REFERENTIAL_RE = re.compile(
    r"那边|那里|那个|这[个条家]|它|上面|刚才|前面|继续|接着|再(?:说|讲|帮)|同样|还是")

# ★ "跑题检查"只对**短上文**生效，这是刻意的。
#   长答案里统计高频 n-gram 得到的"话题特征词"根本不可靠 —— 实测一份
#   1800 字的营养回答，提取得出的特征词全是 markdown 残渣（"。\n\n"、
#   "\n**"），判定于是退化成"两个答案的排版像不像"：排版一变就误报跑题。
#   2026-10-01 真机踩到：一次完全接上上文的追问（答案开头就是"你提到的
#   这两个方法确实很关键"）被判成跑题，白扔了一整次调用 —— 恰恰把省
#   token 这件事做反了。
#   而且正规追问本来就会换一套词（从"汤健不健康"钻到"焯水降不降嘌呤"），
#   词面不重合是常态，不能当跑题证据。
#   短上文（如一句天气）里话题词是明确的，"答错对象"能稳稳抓出来，
#   那才是这套检查真正适用的场景。
_DRIFT_PREV_MAX = 600


def _looks_like_topic_drift(answer: str, prev_answer: str,
                            prompt: str) -> bool:
    """是不是"认错对象"了？

    实测：豆包页面明明还留着上一轮的答案（问的是永城），
    追问"那边有什么好玩的"却整篇在推荐**睢县** —— 上下文没接上，
    比单纯反问更隐蔽，光看不出错。

    判据很朴素：提问用了指代词（那边/它…），而上文里反复出现的特征词
    在答案里一个都没出现 → 大概率答错了对象。
    宁可多疑一次（重试一遍带历史的），也别把跑题的答案交付出去。

    ★ 但这套判据只在**上文够短**时成立（见 _DRIFT_PREV_MAX 的说明）：
    长上文里挑不出可靠的话题词，挑出来的多半是排版噪声，硬判会误伤。

    ★ 调用契约：`prev_answer` 必须是**上一轮答案的全文**。
    传"末尾 60 字"那种片段进来，等于让人从半句话里猜主题 —— 长答案上
    必然误报（pool.ask 里为此专门留了 prev_full，别顺手改成 prev_answer）。
    """
    a = (answer or "").strip()
    p = (prompt or "").strip()
    if not a or not p or not _REFERENTIAL_RE.search(p):
        return False
    if len(a) > 3000:
        return False
    prev = (prev_answer or "").strip()
    if len(prev) < 6 or len(prev) > _DRIFT_PREV_MAX:
        return False
    # 从上一轮答案里挑"话题特征词"。
    # 注意不能用贪婪的中文正则切词 —— 它会把"…为主，永城最核心…"切成
    # "主永城最"这种跨词片段，"永城"反而提取不出来。改用字符 n-gram：
    # 把出现 ≥2 次的 2/3 字片段当作话题核心词（"永城"就会浮出来）。
    #
    # ★ n-gram 必须建在**纯中文**上：直接拿原文切，markdown 的
    #   "**"、"|"、换行会挤满高频名额，特征词就成了排版残渣。
    src = re.sub(r"[^\u4e00-\u9fa5]", "", prev)
    g3 = [src[i:i + 3] for i in range(len(src) - 2)]
    g2 = [src[i:i + 2] for i in range(len(src) - 1)]
    top: list[str] = []
    for g in (g3, g2):                       # 先长后短，长片段更具体
        for w, c in Counter(g).most_common(4):
            if c >= 2 and len(w) >= 2 and w not in top:
                top.append(w)
        if len(top) >= 3:
            break
    # 短文（比如一句天气）里主语往往只出现一次，统计不出高频片段，
    # 所以再补上"上文开头那两个字" —— 它通常就是话题本身（"永城市今日…"→"永城"）。
    head = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", prev)
    if len(head) >= 2 and head[:2] not in top:
        top.append(head[:2])
    top = top[:4]
    if not top:
        return False
    return not any(w in a for w in top)


def _norm_text(s: str) -> str:
    """比对文本前先去掉所有空白。

    页面渲染出来的文本和抓回来的文本，空格/换行/全角半角常有不一致，
    不做归一化就会把"明明还在同一会话"误判成"会话丢了"。
    """
    return re.sub(r"[\s\u3000]+", "", s or "")


def new_thread_id() -> str:
    return time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


async def provider_states() -> list[dict]:
    """给控制台用：每个 provider 的启用/登录/运行状态。"""
    out = []
    for a in provider_pkg.all_adapters():
        conf = settings.provider_conf(a.id)
        running = a.id in browser.manager.running()
        out.append({
            "id": a.id,
            "name": a.name,
            "url": a.url,
            "homepage": a.homepage or a.url,
            "tags": a.tags,
            "badge": a.badge,
            "note": a.note,
            "enabled": bool(conf.get("enabled", True)),
            "running": running,
            "logged_in": None if not running else True,  # 实时状态由 /v1/state 补
            "error": browser.manager.last_error(a.id),
        })
    return out


async def availability(force_check: bool = False) -> dict[str, bool]:
    """哪些 provider 当前可用（已登录）。force_check 会真的去点浏览器。"""
    res: dict[str, bool] = {}
    for a in provider_pkg.all_adapters():
        if not a.enabled():
            res[a.id] = False
            continue
        if not force_check:
            res[a.id] = a.id in browser.manager.running()
            continue
        try:
            page = await browser.manager.ensure_page(a)
            res[a.id] = await a.is_logged_in(page)
        except Exception:
            res[a.id] = False
    return res


async def _try_one(a, prompt: str, *, reset: bool, timeout: float,
                   no_mode: bool = False, grab_images: bool = False,
                   context: str = "", prev_url: str = "",
                   prev_answer: str = "", reuse_session: bool = False,
                   prev_born: float = 0.0, thread: str = "",
                   implicit: bool = False) -> dict:
    page = None
    live = False
    # 停机时直接返回失败，**不要碰浏览器**。批量任务里这一句就是紧急制动：
    # 智能体循环发出去的后续每一问都会在这里被挡下，而不是各起重开一个窗口。
    why = switch.halted()
    if why:
        from .adapter import AskResult
        r = AskResult(False, a.id, error=f"[已停机] {why}", elapsed=0.0)
        return {"ok": False, "provider": a.id, "answer": "", "error": r.error,
                "via": "", "elapsed": 0.0, "mode": {}, "images": []}

    # ---- ① 纯 HTTP 直连优先：不开浏览器，1~2 秒出结果 ----
    #
    # 放在浏览器之前是**唯一有意义的位置** —— 一旦先 ensure_page，
    # 浏览器已经起来了，7~15 秒的固定开销已经花掉，再切 HTTP 就白省了。
    #
    # 失败一律静默退回浏览器（ask_http 内部已把异常吞成 None）：
    # HTTP 是加速路径，不是必经之路，绝不能因为它的失败影响可用性。
    if getattr(a, "http_capable", False):
        # 会话复用策略要和浏览器路线保持一致：调用方说"别复用"
        # （失忆自愈的重试就是这种情况），HTTP 这边也得新开会话，
        # 否则它接着上次的会话问，历史照样没补上、问题依旧。
        http_reset = bool(reset) or not reuse_session
        try:
            hr = await a.ask_http(prompt, thread=thread, reset=http_reset,
                                  timeout=timeout, no_mode=no_mode,
                                  grab_images=grab_images, context=context,
                                  should_stop=switch.halted)
        except HttpStopped as e:
            # ★ 用户叫停。**绝不退回浏览器** —— 退了也是白退（停机闸会把
            #   ensure_page 挡下），真正的问题是日志会写成"直连异常 →
            #   退回浏览器"，让人以为 HTTP 通道坏了（真机实测 2026-10-01）。
            print(f"[http] {a.id} 被叫停（生成中停机，连接已断开）", flush=True)
            return {"ok": False, "provider": a.id, "answer": "",
                    "error": str(e), "via": "", "elapsed": 0.0,
                    "mode": {}, "images": []}
        except Exception as e:  # noqa: BLE001
            print(f"[http] {a.id} 直连异常 → 退回浏览器："
                  f"{type(e).__name__}: {e}", flush=True)
            hr = None
        if hr is not None and hr.ok and hr.answer.strip():
            u = await usage.arecord(a.id, prompt, hr.answer, hr.elapsed, True)
            # 会话页地址：猜的是 DeepSeek 的标准会话 URL。万一哪天格式变了，
            # 浏览器路径拿它去 goto 会失败，于是按"会话丢了"处理、回填历史 ——
            # 仍然是安全方向，不会串话题。
            chat_url = (f"https://chat.deepseek.com/a/chat/s/{hr.session_id}"
                        if hr.session_id else "")
            print(f"[http] {a.id} 直连成功：{hr.elapsed:.2f}s、"
                  f"{len(hr.answer)} 字、接上文={hr.resumed}", flush=True)
            return {
                "ok": True,
                "provider": a.id,
                "provider_name": a.name,
                "answer": strip_noise(hr.answer),
                "error": "",
                "elapsed": round(hr.elapsed, 2),
                "via": hr.via,
                "shot": "",
                "mode": dict(hr.mode or {}),
                "images": [],
                "tokens": u,
                "_chat_url": chat_url,
                "_live": bool(hr.resumed),
                # ★ 标记来源。ask() 里那个"答案和上轮一模一样 → 判定没答出来"
                #   的检查是**为浏览器路线写的**（它靠重新读页面抓答案，
                #   超时就会把上一轮的旧答案当成新答案抓回来）。
                #   HTTP 路线的答案来自本次 SSE 流，不存在这个风险；
                #   若不加区分，同一话题里连着问同样的问题（答案自然相同）
                #   会被误报成"本轮没有产生新回答"。
                "_via_http": True,
            }

    async with browser.manager.lock(a.id):
        try:
            page = await browser.manager.ensure_page(a)
            # 网页端会话还活着吗？**唯一可靠的标准是：页面上还看得见上一轮的答案。**
            #
            # 不能只比 URL —— 实测 DeepSeek 答完常把 URL 导航回首页，
            # 于是"首页 URL == 首页 URL"会被误判成"会话还在"，追问时
            # 不填历史，结果模型一脸懵地反问"你说的'那边'是哪里？"。
            # 反过来只比 URL 也会漏掉"URL 变了但会话还在"的情况。
            # 判据就是**会话页面 URL 有没有变**：
            #   没变 → 网页端自己有上文，直接在同一个会话里接着问，不重发历史；
            #   变了 → 会话多半丢了（浏览器被关 / 站点跳回了首页），才回填历史兜底。
            live = False
            now_born = browser.manager.born(a.id)
            if reuse_session and prev_url and not reset:
                try:
                    # ★ 先看浏览器上下文有没有被重建过。重建了（浏览器被关、
                    #   进程被杀…）页面上就是全新会话，**哪怕 URL 碰巧一样**。
                    #   只看 URL 会在这种情况下误判成"会话还在"，
                    #   于是不带历史去问，模型只能靠猜（实测它把 2 猜对了，
                    #   但那是蒙的，不能依赖）。
                    if prev_born and now_born != prev_born:
                        live = False
                        raise RuntimeError("浏览器上下文已重建")
                    # 判据就用**会话页面 URL 是否没变**。这最直接：
                    # URL 还是那一页 → 网页端自己有上文，直接接着问，不重发历史。
                    # （之前图省事改成"抓页面内容看有没有上一轮答案"，反而因为
                    #   渲染细节对不齐而长期误判成"会话丢了"，每次追问都白带一遍历史。）
                    same = (page.url == prev_url) or (
                        page.url.split("?")[0] == prev_url.split("?")[0])
                    if not same and prev_url.startswith("http"):
                        # URL 变了：可能站点答完自己跳回首页了，试着回到会话页
                        try:
                            await page.goto(prev_url, wait_until="domcontentloaded",
                                            timeout=25000)
                            await page.wait_for_timeout(1500)
                            same = bool(await a.snapshot_answer(page))
                        except Exception:
                            same = False
                    live = bool(same)
                except Exception:
                    pass
            if prev_url and not reset:
                # 只在"本以为是续聊"时打一行，正常新话题不刷屏
                print(f"[thread] 续聊 {a.id}: 复用页面会话={live} | "
                      f"ctx重建={bool(prev_born and now_born != prev_born)} | "
                      f"prev={(prev_url or '')[-12:]!r} cur={page.url[-12:]!r} | "
                      f"{'不重发历史' if live else ('干净开局(隐式话题不回填)' if implicit else '回填历史兜底')}", flush=True)
            if implicit and not live:
                # ★ 隐式话题没有「必须接上」的历史（见 _history_of），所以网页
                #   会话一旦没接上就**干净开局**：既不回填，也不把问题发进那个
                #   残留着上一件事的旧会话页 —— 否则用户照样会看到
                #   「新问题搭着很早以前的对话」。
                sent, reset = prompt, True
            else:
                sent = prompt if live else ((context + prompt) if context else prompt)
            r = await a.ask(page, sent, reset=reset, timeout=timeout, no_mode=no_mode,
                            grab_images=grab_images,
                            stable_window=settings.get()["runtime"]["stable_window"])
            # 浏览器句柄失效（TargetClosedError）时，重建一次再试，
            # 而不是把异常抛给用户。常见于服务被强杀、窗口被手动关掉。
            if not r.ok and ("TargetClosed" in (r.error or "")
                             or "has been closed" in (r.error or "")
                             or "browser has been closed" in (r.error or "")):
                # 浏览器被手动关掉 / 上下文失效：重建后重试。
                # 重试两次并给浏览器一点启动时间 —— 实测用户连点关闭时，
                # 只重试一次会在"刚重建好又被关"的窗口里栽第二次。
                #
                # 但**重试不等于无视用户意图**：
                #   · 停机开关一开，立刻收手（上面的 note_rebuild 会在
                #     反复关闭时自动触发停机，这里自然就被挡住了）
                #   · 无论如何不再多起重开 —— 用户每关一次都要能被感知到
                for _try in range(2):
                    await asyncio.sleep(1.2 * (_try + 1))
                    if switch.halted():
                        from .adapter import AskResult
                        r = AskResult(False, a.id,
                                      error=f"[已停机] {switch.halted()}")
                        break
                    await browser.manager.close(a.id)
                    try:
                        page = await browser.manager.ensure_page(a)
                        r = await a.ask(page, sent, reset=reset, timeout=timeout,
                                        no_mode=no_mode, grab_images=grab_images,
                                        stable_window=settings.get()["runtime"]["stable_window"])
                    except Exception as e2:  # noqa: BLE001
                        from .adapter import AskResult
                        r = AskResult(False, a.id, error=f"{type(e2).__name__}: {e2}")
                    if r.ok:
                        break
        except Exception as e:  # noqa: BLE001
            from .adapter import AskResult
            r = AskResult(False, a.id, error=f"{type(e).__name__}: {e}")

        if not r.ok:
            # 截图要开页面，停机时就别又把浏览器拉起来了
            if not switch.halted():
                try:
                    page = await browser.manager.ensure_page(a)
                    r.shot = await browser.screenshot(page, a.id, "fail")
                except Exception:
                    pass

        # ★ 问完刷新空闲计时（放在这里而不是 ensure_page 那一刻：一次提问
        #   可能跑几十秒，从"问完"起算才准）。HTTP 直连路线在上面就 return 了，
        #   不会走到这里 —— 它没开浏览器，不该替某个闲置窗口续命。
        browser.manager.mark_use(a.id)
        u = await usage.arecord(a.id, prompt, r.answer if r.ok else "",
                                r.elapsed, r.ok)
        return {
            "ok": r.ok,
            "provider": a.id,
            "provider_name": a.name,
            "answer": strip_noise(r.answer) if r.ok else "",
            "error": r.error,
            "elapsed": round(r.elapsed, 2),
            "via": r.via,
            "shot": r.shot,
            "mode": dict(r.mode or {}),   # 深度思考等开关的最终状态
            "images": list(r.images or []),  # 生成式站点产出的图片（本地路径）
            "tokens": u,
            # 内部字段：本次落在哪个会话页、是否复用了页面里的上文
            "_chat_url": (page.url if page is not None else ""),
            "_live": live,
        }


async def last_ok_provider() -> str | None:
    """最近一次成功调用落在哪家 —— 自动路由时优先黏住它，
    否则连续追问会被分到不同站点，上下文就断了。

    异步版：内部走 `usage.aload()`（整读 usage.json），别在事件循环里同步读盘。
    """
    from . import usage
    for h in reversed((await usage.aload()).get("history", [])):
        if h.get("ok"):
            return h.get("provider")
    return None


async def ask(prompt: str, *, provider: str | None = None,
              thread: str | None = None, reset: bool | None = None,
              timeout: float | None = None, fallback: bool = True,
              no_mode: bool = False, grab_images: bool = False) -> dict[str, Any]:
    cfg = settings.get()
    rt = cfg["runtime"]
    # 是否复用网页端页面里的会话（省 token）。
    # **默认开**（`settings.DEFAULTS.runtime.reuse_page_session` 与 config.yaml 都是 true）：
    # 页面还在同一个会话就直接接着问、不重发历史；会话丢了才回填历史兜底。
    # 开着偶尔会"串话题"，但下面有反问/跑题检测兜底、会自动带历史重试一次，
    # 所以默认开着是安全的；想更稳就把 reuse_page_session 设成 false（费 token）。
    # 这里的 `.get(..., True)` 只是防"键被整段删掉"，默认值要和 DEFAULTS 一致，
    # 别写成 False —— 那会和 config.yaml 的注释自相矛盾。
    _reuse_session = bool(rt.get("reuse_page_session", True))
    if timeout is None:
        timeout = float(rt["ask_timeout"])

    _load_threads()

    # --- 先决定实际会用到哪家站点（未指定就按可用性挑一个） ---
    explicit = provider and provider not in ("auto", "")
    resolved = provider if explicit else None
    if not resolved:
        avail0 = await availability()
        ranked0 = router.rank(
            [x for x in provider_pkg.all_adapters() if x.enabled()], avail0, prompt)
        sticky = await last_ok_provider()
        enabled_ids = {x.id for x in provider_pkg.all_adapters() if x.enabled()}
        # 粘性优先：最近成功过的站点继续用，保证追问落在同一个网页会话上。
        # 注意只看"是否启用"——availability 返回的是浏览器是否已打开，不能拿来否决。
        resolved = sticky if (sticky in enabled_ids and not explicit) else (
            ranked0[0].id if ranked0 else None)

    # --- thread 语义 ---
    if thread:
        tid = thread.strip()
    else:
        # 没给话题 → 用「该站点的隐式续聊话题」，连续追问自动连在同一个网页会话上。
        # 想干净开局就显式传 reset=true（控制台的"新话题"按钮就是这么做的）。
        tid = f"auto-{resolved}" if (resolved and reset is None) else ""

    remembered = get_thread(tid) if tid else None
    if remembered:
        # 续聊：不新建会话；若调用方没有指定 provider，沿用上次那家
        if not explicit:
            provider = remembered.get("provider")
        elif remembered.get("provider") and provider != remembered.get("provider"):
            remembered["provider"] = provider   # 换了站点：这个话题转到新站点
        if reset is None:
            reset = False
        if provider != remembered.get("provider") and reset is False:
            return {
                "ok": False,
                "error": f"话题「{remembered.get('title', tid)}」之前在 "
                         f"{remembered.get('provider')} 上聊，换站点就得重开会话。",
                "hint": "要么传 provider 一致，要么传 reset=true 重开。",
            }
    else:
        if reset is None:
            reset = True       # 首次开启话题 → 干净开局
        if tid:
            _touch_thread(tid, resolved or provider or "?", prompt[:24])

    # --- 选 provider ---
    if provider and provider not in ("auto", ""):
        first = provider_pkg.get(provider)
        if not first:
            return {"ok": False, "error": f"未知 provider: {provider}",
                    "available": [x.id for x in provider_pkg.all_adapters()]}
        avail = await availability()
        rest = [x for x in router.rank(
            [x for x in provider_pkg.all_adapters() if x.enabled()],
            avail, prompt) if x.id != provider]
        # 就算指定了 provider，失败后也允许退到别家（fallback 才换）
        targets = [first] + (rest if fallback else [])
    else:
        avail = await availability()
        usable = [x for x in provider_pkg.all_adapters() if x.enabled()]
        targets = router.rank(usable, avail, prompt)

    if not targets:
        return {"ok": False, "error": "没有可用的 provider（都未启用？）"}

    attempts: list[dict] = []
    max_retries = int(rt.get("max_retries", 1)) if fallback else 0
    tried = 0
    origin_provider = remembered.get("provider") if remembered else None
    # 话题历史：**只在网页端会话丢了的时候才回填**（见 _try_one 里的 live 判断）。
    # 会话还在就直接接着问，不重发历史 —— 这也是用户明确要的体验。
    # ★ 能回填的历史由 _history_of 判定：隐式话题恒为空（见该函数说明）。
    history = _history_of(tid, remembered)
    context = _build_context(history)
    ctx_turns = len(history)
    prev_url = (remembered or {}).get("chat_url", "") if remembered else ""
    # 上一轮答案的尾部片段（用于"页面上还看得见上文吗"的比对）
    prev_answer = ""
    # 上一轮答案**全文** —— 跑题检查要用这份。
    # ★ 千万别把 prev_answer 那个"末尾 60 字"喂给跑题检查：它只是给浏览器
    #   路线比对页面残留用的，拿它去统计"话题特征词"等于从半句话里猜主题，
    #   长答案上必然误报（实测一次完全接上上文的追问因此被判跑题、
    #   白扔一整套"新会话 + 回填历史"，恰好把直连省的 token 又花回去）。
    prev_full = ""
    if history:
        prev_full = (history[-1].get("a") or "").strip()
        prev_answer = prev_full[-60:]
    # 上次对话时用的浏览器上下文时间戳；变了说明上下文被重建过
    prev_born = float((remembered or {}).get("ctx_born", 0.0)) if remembered else 0.0
    for a in targets:
        # ★ 停机时不再往下换站点重试。这段重复了一次（每次循环 tried 会 +2），
        #   是早前编辑粘重复的，害得 max_retries 从来没生效过 —— 已修正。
        stopped = switch.halted()
        if stopped:
            return {"ok": False, "error": f"[已停机] {stopped}",
                    "attempts": attempts, "thread": tid or None,
                    "hint": f"要继续就{resume_hint()}。"}
        if tried > max_retries:
            break
        tried += 1
        # 续聊话题换站点时必须重开会话：新站点没有上文，续在旧会话里只会答非所问
        force_reset = bool(origin_provider and a.id != origin_provider)
        res = await _try_one(a, prompt, reset=reset or force_reset,
                             timeout=timeout, no_mode=no_mode,
                             grab_images=grab_images,
                             context=context, prev_url=prev_url,
                             prev_answer=prev_answer,
                             reuse_session=_reuse_session,
                             prev_born=prev_born, thread=tid or "",
                             implicit=_is_implicit_thread(tid))
        attempts.append({k: res[k] for k in ("provider", "ok", "error", "elapsed")})
        if res["ok"]:
            chat_url = res.pop("_chat_url", "")
            live = res.pop("_live", False)
            via_http = bool(res.pop("_via_http", False))
            # ★ 最后一道闸：绝不让「我们自己刚发出去的那段文本」当答案交付。
            #   根因已修在 adapter.looks_like_echo（回填块里的「用户问」曾把
            #   回显判定整个关掉），这里再拦一次 —— 一旦漏过来，它既会交付给
            #   用户，又会被写进话题历史、下一轮再回填下去。
            expected = (prompt if live else
                        ((context + prompt) if context else prompt))
            got = _norm_text(res.get("answer", ""))
            if not via_http and got and (
                    got == _norm_text(expected)
                    or res["answer"].lstrip().startswith(CTX_HEADER[:12])):
                return {
                    "ok": False,
                    "error": f"{a.name} 这轮没抓到回答（抓回来的是我们自己发出去"
                             f"的内容）",
                    "attempts": attempts,
                    "thread": tid or None,
                    "hint": "该站点这轮可能只生成了图片、文字回答没吐出来；"
                            "重试一次，或把 config.yaml 里该站点的 ask_timeout 调大。",
                }
            # 抓回来的还是上一轮的答案 → 说明这次其实没答出来
            # （多半是超时，页面上那条旧答案被当成了新答案）。
            # 宁可明确报错，也不能把上一轮的答案冒充成本轮结果交付出去。
            #
            # 这条检查只对**浏览器路线**成立 —— 它靠重新读页面抓答案，
            # 超时就会读到旧内容。HTTP 路线的答案来自本次 SSE 流，
            # 天然是新的；同一话题里连着问同一句话（答案自然一致）
            # 不该被误判成失败，所以直接跳过。
            if prev_answer and not via_http and \
                    _norm_text(res.get("answer", "")) == _norm_text(prev_answer):
                return {
                    "ok": False,
                    "error": f"{a.name} 本轮没有产生新回答（可能仍在生成，或已超时）",
                    "attempts": attempts,
                    "thread": tid or None,
                    "hint": "该站点这轮答得慢。可以直接重试这一问；"
                            "若经常超时，把 config.yaml 里该站点的 ask_timeout 调大。",
                }
            # 自愈：我们以为"复用了页面会话"，但模型却在反问刚交代过的信息
            # （"你说的'那边'是哪里？"）→ 说明上文其实没接上。
            # 这时把历史回填进去重试一次，而不是把这份失忆的答案交付出去。
            if live and (_looks_like_missing_ctx(res.get("answer", ""))
                         or _looks_like_topic_drift(res.get("answer", ""),
                                                    prev_full, prompt)):
                print(f"[thread] 判定上下文没接上（反问/跑题）→ 带历史重试",
                      flush=True)
                # ★ thread 必须传下去：重试会**新开一个远端会话**（reuse_session=False），
                #   而不传 thread 的话 http_deepseek 就不会 remember_session，
                #   新会话等于白开 —— 话题指针还停在那个"模型没接上上文"的旧会话上，
                #   下一次追问又去接它，历史还得再回填一遍。
                retry = await _try_one(a, prompt, reset=False, timeout=timeout,
                                       no_mode=no_mode, grab_images=grab_images,
                                       context=context, prev_url="",
                                       reuse_session=False, thread=tid or "",
                                       implicit=_is_implicit_thread(tid))
                r_url = retry.pop("_chat_url", "")
                retry.pop("_live", None)
                if retry.get("ok") and retry.get("answer") \
                        and not _looks_like_missing_ctx(retry["answer"]) \
                        and not _looks_like_topic_drift(retry["answer"],
                                                        prev_full, prompt):
                    res = retry
                    chat_url = r_url or chat_url
                    live = False
                    res["recovered"] = True
            if tid:
                _touch_thread(tid, a.id, prompt[:24], chat_url,
                              browser.manager.born(a.id))
                # 隐式话题不记内容：它不参与回填（见 _history_of），
                # 记下来只会让 threads.json 越滚越大、还在控制台里骗人。
                if not _is_implicit_thread(tid):
                    _append_message(tid, prompt, res["answer"], a.id, chat_url)
            res["attempts"] = attempts
            res["thread"] = tid or None
            res["routed"] = provider or "auto"
            res["continued"] = bool(remembered)
            # reused_page=True 表示"在网页端同一个会话里接着问"，
            # False 表示会话丢了、这次是把历史回填进去的
            res["reused_page"] = bool(live)
            res["context_turns"] = 0 if live else ctx_turns
            return res

    last = attempts[-1] if attempts else {}
    err = last.get("error") or "所有 provider 均失败"
    if err.startswith("[已停机]"):
        # 停机不能提示"直接重试" —— 那正好是用户最不想听到的
        hint = ("任务已停机，本次及后续调用都被拦下了，浏览器不会再被拉起。\n"
                f"恢复的办法：{resume_hint()}。")
    elif "TargetClosed" in err or "has been closed" in err:
        hint = ("浏览器窗口被关掉了（也可能是你手动关的）。"
                "服务会在下次调用时自动重开并接着上次的上下文，**直接重试这一问即可**。")
    else:
        hint = "先在控制台对这些站点执行「登录」，或检查是否触发了验证码/风控"
    return {
        "ok": False,
        "error": err,
        "attempts": attempts,
        "thread": tid or None,
        "hint": hint,
    }
