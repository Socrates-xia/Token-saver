"""设置加载：内置默认值 + 可选 YAML 覆盖。

用户可以直接编辑 token-saver/config.yaml 来改 provider 列表、选择器、
参考单价等，不需要动 Python 代码。

运行期状态放在**代码目录之外**
------------------------------
技能包要能整包上传/分发，所以包内不能出现任何运行期产物 —— 登录态、
浏览器 profile、截图、usage 统计、话题记录…… 全都落在

    $TOKEN_SAVER_HOME/data/      （未设该环境变量时为 ~/.token-saver/data/）

这样做有两个直接好处：

1. **打包干净**：上架校验会检查包内是否含凭证、目录层级是否超限；
   装了 .venv 或 data/ 的技能包一定过不了，而运行一次就会生成它们。
   状态外置之后，代码目录可以永远保持"刚解压"的样子。
2. **升级不丢登录**：换一版技能包只要覆盖代码目录，登录态原样还在。

早先版本把 data/ 放在代码目录里，首次运行会自动**复制**过去迁移
（copy 而不是 move，万一迁移出问题旧数据还完整躺着）。
"""
from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

# 代码根（= 技能包目录，里面有 core/ web/ tools/ server.py …）
ROOT = Path(__file__).resolve().parent.parent
# 随包分发的静态资源（不放运行期产物）
ASSETS_DIR = ROOT / "assets"
CONFIG_FILE = ROOT / "config.yaml"


def _resolve_home() -> Path:
    """运行期状态根目录。"""
    env = (os.environ.get("TOKEN_SAVER_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    try:
        return Path.home() / ".token-saver"
    except Exception:  # noqa: BLE001  （拿不到 HOME 时的极端兜底）
        return ROOT / ".token-saver-home"


HOME = _resolve_home()
DATA_DIR = HOME / "data"
PROFILE_DIR = DATA_DIR / "profiles"
SHOT_DIR = DATA_DIR / "shots"
IMAGE_DIR = DATA_DIR / "images"   # 从生成式站点抓回来的图片
# 程序改动落在这里，不碰 config.yaml（否则会冲掉注释）
OVERRIDES_FILE = DATA_DIR / "overrides.yaml"
USAGE_FILE = DATA_DIR / "usage.json"

# 老版本把运行数据放在代码目录里，这里保留路径用于一次性迁移
LEGACY_DATA_DIR = ROOT / "data"


def _migrate_legacy_data() -> None:
    """把老版本的 `<代码根>/data/` 复制到新的 HOME/data/。

    只在"新位置还没有任何状态"且"老位置确实有东西"时才动手，
    并且是**复制**：迁移失败或用户后悔都能原样退回。
    """
    try:
        if DATA_DIR.resolve() == LEGACY_DATA_DIR.resolve():
            return
        if not LEGACY_DATA_DIR.is_dir():
            return
        if (DATA_DIR / "state").exists() or (DATA_DIR / "profiles").exists():
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        copied = 0
        for item in LEGACY_DATA_DIR.iterdir():
            dst = DATA_DIR / item.name
            if dst.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
            copied += 1
        if copied:
            print(f"[settings] 已把运行数据迁移到 {DATA_DIR}"
                  f"（旧目录 {LEGACY_DATA_DIR} 保留未删，确认无误后可自行删除）",
                  flush=True)
    except Exception as e:  # noqa: BLE001  迁移失败不该拦住服务启动
        print(f"[settings] 运行数据迁移跳过：{type(e).__name__}: {e}", flush=True)


for _d in (DATA_DIR, PROFILE_DIR, SHOT_DIR, IMAGE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
_migrate_legacy_data()


DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8787,
    },
    "browser": {
        # msedge / chrome / None(用 playwright 自带 chromium，需先 install)
        "channel": "msedge",
        # 网页端站点普遍有反自动化检测，默认有头运行更稳
        "headless": False,
        "slow_mo": 0,
        "locale": "zh-CN",
    },
    "runtime": {
        "ask_timeout": 180,          # 单次提问最长等待（秒）
        "stable_window": 2.6,        # 文本稳定多久判定生成结束（秒）
        "reset_mode": "always",      # always | never —— 每次提问前是否新开会话
        "max_retries": 1,
        # 追问时是否复用网页端页面里的会话（页面还在就不重发历史，省 token）。
        # 默认 True —— 这是用户要的体验：**没关浏览器就是纯追问**。
        # 风险：个别站点会出现"串话题"（页面明明有上文，模型却答错对象）。
        # 这种事由 _looks_like_missing_ctx / _looks_like_topic_drift 抓出来，
        # 抓到就自动带历史重试一次，兜住它。
        "reuse_page_session": True,
        # 支持直连的站点（目前只有 DeepSeek）优先走纯 HTTP，不开浏览器。
        # 首字延迟 7~15s → 1~2s，失败自动退回浏览器，所以默认开着。
        # 想全体关掉（比如要观察浏览器行为）把它设成 false。
        "http_first": True,
        # 空闲自动关闭窗口：最后一次用到某个站点之后，闲置超过这么多秒就把它的
        # 浏览器窗口关掉 —— 用户要的体验是"不用再聊了就别留窗口，别让我手动关"。
        #   0 = 关掉这个功能，窗口一直留着。
        # 为什么是 180 秒：足够覆盖"读完答案接着追问"（窗口内追问会续上计时，
        # 走最省 token 的复用路线），又不至于让窗口长期赖着。
        # 被关掉不影响正确性 —— 下次调用会自动重开、按话题类型决定回填历史，
        # 只是多花几秒启动。想立刻关用 POST /api/close（MCP: close_windows）。
        "idle_close_seconds": 180,
    },
    # 省钱估算：把这笔调用当成走主模型本来要花多少钱
    "pricing": {
        "reference_input_usd_per_mtok": 3.0,
        "reference_output_usd_per_mtok": 15.0,
        "reference_model": "主模型（默认按 Sonnet 级估算）",
    },
    "routing": {
        # 复杂→仍需主模型；这里的阈值用于 /v1/route 的复杂度打分
        "simple_max_chars": 1200,
        "heavy_keywords": [
            "架构", "重构", "debug", "调优", "证明", "推导", "多文件",
            "refactor", "architect", "step by step", "chain of thought",
        ],
    },
    # 每个 provider 的开关与选择器增量覆盖
    "providers": {},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load() -> dict:
    """DEFAULTS ← config.yaml ← overrides.yaml

    overrides.yaml 是**程序自己写的**（比如你在控制台点了某个站点的启用开关）。
    刻意跟 config.yaml 分开：config.yaml 是人手写的、带注释的文档，
    用 yaml.dump 回写会把注释全冲掉，所以程序不许碰它。
    """
    out = copy.deepcopy(DEFAULTS)
    for f in (CONFIG_FILE, OVERRIDES_FILE):
        if not f.exists():
            continue
        try:
            raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            # 别静默吞掉 —— 用户手写的配置格式写错时，
            # 如果什么都不说，他会以为改了配置却没生效，极难排查。
            print(f"[settings] 配置文件解析失败、已跳过：{f} → {e}", flush=True)
            continue
        if isinstance(raw, dict):
            out = deep_merge(out, raw)
    return out


_settings: dict | None = None


def get() -> dict:
    global _settings
    if _settings is None:
        _settings = load()
    return _settings


def reload() -> dict:
    global _settings
    _settings = load()
    return _settings


def provider_conf(pid: str) -> dict:
    return get().get("providers", {}).get(pid, {})
