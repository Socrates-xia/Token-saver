"""浏览器上下文管理。

每个 provider 一个独立持久化 profile（user-data-dir），登录态长期保存。
优先复用本机 Edge / Chrome，避免下载上百 MB 的 playwright 内核。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from . import settings

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
try {
  Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
} catch (e) {}
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : origQuery(p);
}
"""


class BrowserManager:
    def __init__(self):
        self._pw: Playwright | None = None
        self._start_lock = asyncio.Lock()
        self._ctx: dict[str, BrowserContext] = {}
        self._page: dict[str, Page] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_error: dict[str, str] = {}
        # 每个站点的浏览器上下文"出生时间"。它一旦变化，就说明这个上下文
        # 被重建过（浏览器被关、进程被杀…）—— 此时页面上的会话一定没了，
        # **哪怕 URL 碰巧还是同一个会話页**。追问时靠它判断要不要回填历史。
        self._born: dict[str, float] = {}
        # ★ 空闲自动关闭用的两张小表（见 close_idle）：
        #   _last_use    最后一次"真正用到这个站点的浏览器"的时间戳；
        #   _idle_closed 最近一次被自动关掉的时间（只用于排障/控制台展示）。
        self._last_use: dict[str, float] = {}
        self._idle_closed: dict[str, float] = {}

    def born(self, pid: str) -> float:
        """该站点浏览器上下文的创建时间戳（0 = 还没建过）。"""
        return self._born.get(pid, 0.0)

    def mark_use(self, pid: str) -> None:
        """记一次"用到了这个站点的浏览器"。空闲计时从这里重新起算。

        调用点有两处够了：`context()`（真要拿页面时）和 `pool._try_one()` 问完
        之后。**HTTP 直连路线不记** —— 它压根没开浏览器，记了只会让一个早就
        没人用的窗口赖着不走。
        """
        self._last_use[pid] = time.time()

    def idle_for(self, pid: str) -> float:
        """该站点已经闲置了多少秒（-1 = 从未记录过）。"""
        t = self._last_use.get(pid)
        return -1.0 if t is None else (time.time() - t)

    def idle_report(self) -> dict:
        """给 `/api/status` 看：谁闲着、闲了多久、最近自动关过谁。"""
        return {
            "idle_for": {p: round(self.idle_for(p), 1) for p in self._ctx},
            "auto_closed": {k: round(v, 1) for k, v in self._idle_closed.items()},
        }

    async def _ensure_playwright(self) -> Playwright:
        async with self._start_lock:
            if self._pw is None:
                self._pw = await async_playwright().start()
            return self._pw

    def lock(self, pid: str) -> asyncio.Lock:
        if pid not in self._locks:
            self._locks[pid] = asyncio.Lock()
        return self._locks[pid]

    def last_error(self, pid: str) -> str:
        return self._last_error.get(pid, "")

    async def _alive(self, pid: str) -> bool:
        """缓存里的上下文/页面是否还活着。

        服务被强杀、用户手动关掉 Edge、站点崩溃之后，缓存的句柄会指向
        已经死掉的目标，再去操作就会报 TargetClosedError。所以每次取用
        前先探活，不活就丢弃重建。"""
        ctx, page = self._ctx.get(pid), self._page.get(pid)
        if ctx is None or page is None:
            print(f"[alive] pid={pid} 无缓存句柄", flush=True)
            return False
        try:
            # ★ 注意：page.is_closed() 在 Playwright 里是**同步**方法，
            # 千万不能 await —— 写成 `await page.is_closed()` 会抛
            # TypeError("object bool can't be used in 'await' expression")，
            # 被下面的 except 吞掉之后就**永远判定"页面已失效"**，
            # 于是每次调用都关掉浏览器重建：
            #   · 网页端会话全丢，追问只能靠回填历史硬撑
            #   · 用户肉眼看到的就是"网页关了又开"
            #   · 每次还要重新加载站点，白等好几秒
            closed = page.is_closed()
        except Exception as e:  # noqa: BLE001
            print(f"[alive] pid={pid} is_closed() 抛错 → 判失效: {e!r}", flush=True)
            return False
        if closed:
            print(f"[alive] pid={pid} 页面确实已关闭 → 判失效", flush=True)
            return False
        try:
            _ = len(ctx.pages)          # context 被关时访问属性会抛错
        except Exception as e:  # noqa: BLE001
            print(f"[alive] pid={pid} ctx.pages 抛错 → 判失效: {e!r}", flush=True)
            return False
        return True

    async def _drop(self, pid: str) -> None:
        ctx = self._ctx.pop(pid, None)
        self._page.pop(pid, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass

    # ------------------------------------------------------------ 上下文
    async def context(self, pid: str, *, headless: bool | None = None
                      ) -> tuple[BrowserContext, Page]:
        # ★ 停机期间连浏览器都不许起。放在这里是因为它是所有调用路径的
        #   必经之地（ask / 抓图 / 登录 / 排障全都走这儿）。
        from .killswitch import resume_hint, switch
        why = switch.halted()
        if why:
            raise RuntimeError(f"[已停机] {why}\n恢复：{resume_hint()}。")
        if await self._alive(pid):
            self.mark_use(pid)      # 空闲计时从"最后一次真用到它"起算
            return self._ctx[pid], self._page[pid]

        # ★ 只有**重建**才计入"用户在连点关闭窗口"的判定，首次启动不算。
        #
        # 这里踩过一次（2026-10-01）：原来把 note_rebuild() 放在 _alive 之后
        # 无条件调用，于是"首次启动"也在计数。后果有两层：
        #   1) 正常的多站点兜底：一问在 60 秒内先后拉起 3 家站点 → 直接误判成
        #      "你在试图中止任务"并自动停机。用户看到的是莫名其妙被停机。
        #   2) **并行 fan-out 完全没法用** —— 同时拉起 3~4 家必然触发停机，
        #      而 fan-out 的全部价值就在于同时开多家。
        # 判据本来就现成：`_page.get(pid)` 有值说明之前开过（现在死了）
        # 才算"重建"；为空是"这个站点本来就没开过"。
        is_rebuild = self._page.get(pid) is not None
        if is_rebuild:
            stopped = switch.note_rebuild()
            if stopped:
                raise RuntimeError(f"[已停机] {stopped}")
            print(f"[ctx] pid={pid} 重建浏览器上下文（页面被关或失效）", flush=True)
        else:
            print(f"[ctx] pid={pid} 首次启动浏览器", flush=True)
        await self._drop(pid)           # 句柄已失效，丢弃后重建

        cfg = settings.get()
        bcfg = cfg["browser"]
        pw = await self._ensure_playwright()
        profile_dir = settings.PROFILE_DIR / pid
        profile_dir.mkdir(parents=True, exist_ok=True)

        if headless is None:
            headless = bool(bcfg.get("headless"))

        launch_kwargs: dict[str, Any] = dict(
            user_data_dir=str(profile_dir),
            headless=headless,
            no_viewport=True,
            locale=bcfg.get("locale", "zh-CN"),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--no-default-browser-check",
                # 抑制 Edge 新 profile 的各种引导弹窗
                "--disable-sync",
                "--disable-signin-promo",
                "--disable-profile-resetter",
                "--disable-session-crashed-bubble",
                "--disable-client-side-phishing-detection",
                "--disable-features=Translate,OptimizationHints,"
                "InterestFeedContentSuggestions,DialMediaRouteProvider,"
                "EdgeSyncIntro,EdgeWelcome,msEdgeAccountStorage",
                "--mute-audio",
                "--disable-notifications",
            ],
        )
        channel = bcfg.get("channel")
        if headless:
            # headless 下部分站点会拒绝 Edge channel 的混合模式，走默认内核更稳
            if channel:
                launch_kwargs["channel"] = channel
        elif channel:
            launch_kwargs["channel"] = channel

        if bcfg.get("slow_mo"):
            launch_kwargs["slow_mo"] = int(bcfg["slow_mo"])

        self._last_error.pop(pid, None)
        try:
            ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:  # noqa: BLE001
            # 本机没有指定的浏览器 → 退回 playwright 内核（需 playwright install chromium）
            first = f"{type(e).__name__}: {e}"
            self._last_error[pid] = f"channel 启动失败，尝试默认内核：{first}"
            launch_kwargs.pop("channel", None)
            try:
                ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
            except Exception as e2:  # noqa: BLE001
                # ★ 两个错误都要报出来。
                #   只报第二个的话，用户看到的是"playwright 需要下载浏览器"，
                #   于是跑去装 chromium —— 但真正挂掉的是 Edge
                #   （实测最常见的原因是**上一次的 Edge 窗口还没退干净**，
                #   user-data-dir 被占用，重启太快就会撞上）。
                #   只给后一个错误等于把人往错误方向引。
                raise RuntimeError(
                    f"浏览器启动失败。\n"
                    f"  1) 指定内核（channel={channel or '未设置'}）失败：{first}\n"
                    f"  2) 退回 playwright 内置内核也失败："
                    f"{type(e2).__name__}: {e2}\n"
                    "若第 1 条是 user data directory is already in use / "
                    "ProcessSingleton 之类的占用错误，多半是上一次的浏览器窗口"
                    "还没退干净 —— 等几秒重试即可，不用重装内核。"
                ) from e2

        await ctx.add_init_script(STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        self._ctx[pid], self._page[pid] = ctx, page
        self._born[pid] = time.time()       # 记下出生时间，供"会话是否重建过"判断
        self.mark_use(pid)
        return ctx, page

    # ------------------------------------------------------------ 页面准备
    async def _prune_pages(self, ctx: BrowserContext) -> Page:
        """只保留主页面，清掉误开的多余标签（比如点'新建对话'弹出的新 tab）。"""
        try:
            pages = [p for p in ctx.pages]
        except Exception:
            pages = []
        if not pages:
            return await ctx.new_page()
        main = pages[0]
        for extra in pages[1:]:
            try:
                if (extra.url or "").startswith(("about:", "chrome:")):
                    await extra.close()
            except Exception:
                pass
        return main

    async def ensure_page(self, adapter, *, headless: bool | None = None) -> Page:
        ctx, page = await self.context(adapter.id, headless=headless)
        # session cookie 在上次关闭时被丢了，这里趁 goto 之前补回去。
        # 必须在导航前做 —— 已经加载好的页面再注入 cookie 得刷新才生效。
        await self._restore_session(ctx, adapter.id)
        page = await self._prune_pages(ctx)
        self._page[adapter.id] = page
        target = adapter.url
        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        if target not in cur:
            await page.goto(target, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(int(adapter.warmup_wait * 1000))
        return page

    async def open_login(self, adapter, *, headless: bool | None = None) -> str:
        """打开该站点的登录/首页，让用户手动扫码登录。"""
        ctx, page = await self.context(adapter.id, headless=headless)
        await self._bring_front(page)
        url = adapter.homepage or adapter.url
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return f"已打开 {adapter.name}（{url}），请在弹出的浏览器窗口中完成登录。"

    # ------------------------------------------------------- 登录态续命
    # 各家几乎都把会话凭证放在 session cookie 里（实测 DeepSeek 的
    # ds_session_id 就是 persistent=0），浏览器一关就被浏览器自己丢掉 ——
    # 于是每次重启都要重新扫码，这个工具就没法长期用了。
    #
    # 办法是把 cookie 的值导出来存盘，下次启动时**赶在导航之前**注回去。
    # 服务端只认 token 本身是否有效，并不知道中间隔了一次浏览器重启，
    # 所以只要服务端没让 token 过期，这条路就走得通。
    @staticmethod
    def _state_file(pid: str) -> Path:
        return settings.DATA_DIR / "state" / f"{pid}.json"

    async def save_session(self, pid: str) -> str:
        """登录后调用：把当前登录态存盘。返回文件路径，失败返回空。"""
        ctx = self._ctx.get(pid)
        if ctx is None:
            return ""
        try:
            f = self._state_file(pid)
            f.parent.mkdir(parents=True, exist_ok=True)
            state = await ctx.storage_state(path=str(f))
            n = len(state.get("cookies") or [])
            print(f"[session] 已保存 {pid} 登录态（{n} 个 cookie）→ {f.name}",
                  flush=True)
            return str(f)
        except Exception as e:  # noqa: BLE001
            print(f"[session] 保存失败 {pid}：{type(e).__name__}: {e}", flush=True)
            return ""

    async def _restore_session(self, ctx: BrowserContext, pid: str) -> bool:
        """把上次存下来的 cookie 注回上下文。**必须在 goto 之前**调用。"""
        if getattr(ctx, "_session_restored", False):
            return False                      # 每个上下文只补一次
        f = self._state_file(pid)
        if not f.exists():
            ctx._session_restored = True      # type: ignore[attr-defined]
            return False
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            cookies = [c for c in (data.get("cookies") or []) if c.get("name")]
            if not cookies:
                return False
            await ctx.add_cookies(cookies)
            ctx._session_restored = True      # type: ignore[attr-defined]
            print(f"[session] 已回填 {pid} 登录态（{len(cookies)} 个 cookie）",
                  flush=True)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[session] 回填失败 {pid}：{type(e).__name__}: {e}", flush=True)
            return False

    async def _bring_front(self, page: Page) -> None:
        try:
            await page.bring_to_front()
        except Exception:
            pass

    # ------------------------------------------------------------ 关闭
    async def close(self, pid: str) -> None:
        ctx = self._ctx.pop(pid, None)
        self._page.pop(pid, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass

    async def close_all(self) -> None:
        for pid in list(self._ctx.keys()):
            await self.close(pid)
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def close_idle(self, idle_seconds: float) -> list[str]:
        """把"闲着没人用"的站点窗口关掉，返回实际关掉的 pid 列表。

        这是"不用再聊了就把窗口收掉"的执行者 —— 由 server 的空闲巡检每隔几秒
        调一次，用户不用再手动关窗口。三条安全线缺一不可：

        1. **正在提问的站点绝不关。** 一个站点只有一个页面，提问路径靠
           `lock(pid)` 排队；关它等于把别人手上正在用的 page 抽走，报出来的
           是随机的 TargetClosedError。所以持锁的一律跳过 —— 而且只读
           `.locked()`，**不去 await 拿锁**，免得巡检自己卡在第一家上。
        2. **不知道上次用时的，先记成"刚用过"** 再跳过。宁可多留一轮，
           也不要把一个来路不明（可能是别人代码路径开的）的窗口误关。
        3. `idle_seconds <= 0` → 整个功能关掉，直接返回。

        注意：窗口关掉**不影响登录态**（cookie 存在持久化 profile 里），
        下次调用会自动重开并回填登录态，只是要多花几秒启动。
        """
        if idle_seconds <= 0:
            return []
        now = time.time()
        closed: list[str] = []
        for pid in list(self._ctx.keys()):
            last = self._last_use.get(pid)
            if last is None:
                self._last_use[pid] = now
                continue
            if now - last < idle_seconds:
                continue
            lock = self._locks.get(pid)
            if lock is not None and lock.locked():
                continue
            await self.close(pid)
            self._last_use.pop(pid, None)
            self._idle_closed[pid] = now
            closed.append(pid)
            print(f"[idle] pid={pid} 已闲置 {int(now - last)}s "
                  f"→ 自动关闭窗口", flush=True)
        return closed

    def running(self) -> list[str]:
        return list(self._ctx.keys())


manager = BrowserManager()


async def screenshot(page: Page, pid: str, tag: str = "") -> str:
    name = f"{pid}-{int(time.time())}{'-' + tag if tag else ''}.png"
    path = settings.SHOT_DIR / name
    try:
        await page.screenshot(path=str(path), full_page=False)
        return name
    except Exception:
        return ""
