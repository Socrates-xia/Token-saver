"""停机开关：让用户（和上层智能体）能真正叫停批量任务。

为什么需要它
------------
正常情况下"浏览器被关 → 自动重建重开"是个好特性（服务被强杀、窗口崩了
能自愈）。但它有个反面：**用户想中止任务时，怎么关它都会爬起来接着干**。

批量调试时这尤其难受：智能体在循环里连着问二十个问题，用户想停下，
关掉浏览器只是让当前这一问重试一次，剩下十九个照跑不误。

于是这里提供三条停机通路 + 一种自动识别：

    1. 文件哨兵   <DATA_DIR>/STOP 存在即停机（最土但最管用，看门狗式），
                  **删掉它就等于恢复**（和另外两条通路语义一致）
    2. HTTP       POST /api/stop   /   POST /api/resume
    3. MCP 工具   stop_offload / resume_offload
    4. 自动       短时间内浏览器被反复关闭 → 判定用户想停 → 自动停机

停机时**绝不启动浏览器**，且正在进行的等待（最长 3 分钟那种）会在
秒级中断，不用等它跑完。
"""
from __future__ import annotations

import time

from . import settings

STOP_FILE = settings.DATA_DIR / "STOP"


class HaltedError(RuntimeError):
    """停机期间试图做浏览器操作。语义明确，便于上层转成干净的错误文案。"""


class KillSwitch:
    def __init__(self) -> None:
        self.reason: str = ""
        self.since: float = 0.0
        # 浏览器重建时间戳，用来识别"用户在反复关闭窗口"
        self._rebuilds: list[float] = []
        self._file_checked_at: float = 0.0
        # 当前这次停机是不是"由哨兵文件承载"的。只有它为 True 时，
        # "文件消失"才等价于"用户要恢复"（见 halted 里的说明）。
        self._from_file: bool = False

    # ------------------------------------------------------------ 查询
    def halted(self) -> str:
        """停机则返回原因，正常则返回空串。

        **删除哨兵文件 = 恢复运行。** 这条语义以前是坏的：`halted()` 只处理
        "文件存在"，文件被删掉时从不 `clear()`，于是内存里的 `self.reason`
        常驻 —— 实测 `rm data/STOP` 之后 `/api/status` 仍报 `halted:true`，
        `/api/ask` 也继续拒绝，而文档和报错文案都写着"删掉 STOP 即可恢复"，
        等于把人指进死胡同（只有 `/api/resume` 能救回来）。
        """
        # 文件哨兵每 0.5s 才查一次磁盘，避免每次轮询都 stat
        now = time.time()
        if now - self._file_checked_at > 0.5:
            self._file_checked_at = now
            if STOP_FILE.exists():
                try:
                    txt = STOP_FILE.read_text(encoding="utf-8").strip()
                except Exception:  # noqa: BLE001
                    txt = ""
                if not self.reason:
                    self.reason = txt or f"检测到哨兵文件 {STOP_FILE.name}"
                    self.since = self.since or now
                self._from_file = True
            elif self._from_file and self.reason:
                # 文件没了，而且这次停机本来就是靠文件承载的 → 用户要复工。
                # 只认"我们自己落过盘"的情形，避免把 halt() 里
                # "先设 reason、后写文件"这两步之间的瞬间当成已恢复。
                self.clear()
        return self.reason

    def clear(self) -> None:
        self.reason = ""
        self.since = 0.0
        self._from_file = False
        self._rebuilds.clear()
        try:
            STOP_FILE.unlink()
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass

    def check(self) -> None:
        """停机就抛错。凡是可能开浏览器的地方都该先调它。"""
        why = self.halted()
        if why:
            raise HaltedError(why)

    def status(self) -> dict:
        why = self.halted()
        return {
            "halted": bool(why),
            "reason": why,
            "since": self.since,
            "stop_file": str(STOP_FILE),
            "destroyed_stop_file_resumes": True,
            "recent_rebuilds": len(self._rebuilds),
        }

    # ------------------------------------------------------------ 自动识别
    def note_rebuild(self) -> str:
        """记一次浏览器重建。返回非空表示已自动触发停机。

        判定窗口 60 秒内重建满 3 次 —— 这是"用户在反复关窗口"的特征：
        正常崩溃自愈不会这么频繁，而连点关闭的用户一定会踩到。
        """
        now = time.time()
        self._rebuilds = [t for t in self._rebuilds if now - t < 60.0]
        self._rebuilds.append(now)
        if len(self._rebuilds) >= 3:
            self.reason = (
                "60 秒内浏览器被关闭并重建了 "
                f"{len(self._rebuilds)} 次 —— 判定为你在试图中止任务，已自动停机。\n"
                f"确认要继续的话：{resume_hint()}。"
            )
            self.since = self.since or now
            self._write_sentinel()
            return self.reason
        return ""

    def _write_sentinel(self) -> None:
        """把停机状态落到文件上，这样即使服务重启也不会悄悄复工。"""
        try:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            STOP_FILE.write_text(
                self.reason or "手动停机", encoding="utf-8")
            self._from_file = True
        except Exception:  # noqa: BLE001
            pass


switch = KillSwitch()


def resume_hint() -> str:
    """统一的"怎么恢复"文案 —— 全项目只有这一处，别再各写各的。"""
    return (f"删掉哨兵文件 {STOP_FILE}，"
            "或 POST /api/resume（MCP 工具 resume_offload）")


def halt(reason: str = "手动停机") -> str:
    """停机。"""
    switch.reason = reason
    switch.since = switch.since or time.time()
    switch._write_sentinel()
    return switch.reason


def resume() -> None:
    switch.clear()

