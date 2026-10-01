#!/usr/bin/env python3
"""Token Saver 一键安装向导。

做四件事：
  1. 检查 Python 版本
  2. 在**技能目录之外**建独立虚拟环境（默认 ~/.token-saver/venv）
  3. 安装依赖（7 个包）
  4. 检查本机浏览器（复用 Edge/Chrome，不下载 Playwright 内核）
最后打印启动方式和可直接粘贴的 MCP 配置片段。

用法：
    python scripts/install.py

为什么虚拟环境不放在技能目录里
------------------------------
技能包要能整包分发/上架，包内一旦出现 `.venv/`（187 MB、近三千个文件）
就会：目录层级超限、被扫描出上万条"疑似凭证"、体积暴涨。
而只要有人在这台机器上跑过它，`.venv/` 就会出现 —— 除非它本来就不在那儿。
所以代码、环境、数据三者分开放：

    代码    <技能目录>                 （只读性质，覆盖升级）
    环境    ~/.token-saver/venv/       （本脚本创建）
    数据    ~/.token-saver/data/       （服务运行时自己创建）

`TOKEN_SAVER_HOME` 可以把后两者换到别处。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 技能目录
REQ = ROOT / "requirements.txt"

IS_WIN = os.name == "nt"


def home_dir() -> Path:
    env = (os.environ.get("TOKEN_SAVER_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".token-saver"


HOME = home_dir()
VENV = HOME / "venv"
DATA = HOME / "data"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def find_browser() -> str | None:
    """找一个可用的 Chromium 系浏览器。"""
    cands: list[Path] = []
    if IS_WIN:
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            cands += [
                Path(base) / "Microsoft/Edge/Application/msedge.exe",
                Path(base) / "Google/Chrome/Application/chrome.exe",
            ]
    elif sys.platform == "darwin":
        cands += [
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        from shutil import which
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            p = which(name)
            if p:
                cands.append(Path(p))
    for c in cands:
        if c.exists():
            return str(c)
    return None


def make_json_path(p: Path) -> str:
    """给 JSON 用的路径：统一正斜杠。

    Windows 的反斜杠直接粘进 JSON 会变成转义字符（`\\U` 之类），
    配置当场就坏了 —— 而且报错信息完全看不出是路径写法的问题。
    """
    return str(p).replace("\\", "/")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    say("=" * 60)
    say("  Token Saver 安装向导")
    say("  把简单任务外包给网页端免费 AI，省 token")
    say("=" * 60)

    # ---- 1. Python 版本
    ver = sys.version_info
    say(f"\n[1/4] Python 版本：{ver.major}.{ver.minor}.{ver.micro}")
    if ver < (3, 10):
        say("  ✗ 需要 3.10 及以上，请先升级 Python")
        return 1
    say("  ✓ 没问题")

    if not REQ.exists():
        say(f"  ✗ 找不到依赖清单：{REQ}（技能目录不完整？）")
        return 1

    # ---- 2. 虚拟环境（放在技能目录之外）
    say(f"\n[2/4] 虚拟环境：{VENV}")
    say("      （刻意不放在技能目录里：包里出现 .venv 会让整包分发/上架失败，")
    say("        而且它绑定了本机绝对路径，本来也不能传播）")
    if venv_python().exists():
        say("  ✓ 已存在，跳过创建")
    else:
        say("  创建中…")
        try:
            subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        except subprocess.CalledProcessError as e:
            say(f"  ✗ 创建失败：{e}")
            if not IS_WIN:
                say("    多数 Linux 发行版需要先装：sudo apt install python3-venv")
            return 1
        say("  ✓ 完成")

    # ---- 3. 依赖
    say("\n[3/4] 安装依赖（fastapi / uvicorn / playwright / pyyaml / mcp / httpx / wasmtime）")
    try:
        subprocess.run(
            [str(venv_python()), "-m", "pip", "install", "-q",
             "--disable-pip-version-check", "-r", str(REQ)],
            check=True,
        )
        say("  ✓ 完成")
    except subprocess.CalledProcessError as e:
        say(f"  ✗ 安装失败：{e}")
        say("    可以手动跑：")
        say(f"    {venv_python()} -m pip install -r {REQ}")
        return 1

    # ---- 4. 浏览器
    say("\n[4/4] 检查本机浏览器（复用它，不下载额外内核）")
    br = find_browser()
    if br:
        say(f"  ✓ 找到：{br}")
    else:
        say("  ! 没找到 Edge / Chrome")
        say("    装一个 Edge 或 Chrome 即可；若坚持用 Playwright 自带内核，")
        say(f"    需要额外执行：{venv_python()} -m playwright install chromium")
        say("    （会下载 100+ MB，不推荐）")

    # ---- 完成
    py = venv_python()
    say("\n" + "=" * 60)
    say("  安装完成，接下来三步：")
    say("=" * 60)
    say("\n1) 启动服务：")
    say(f"   {py} -B {ROOT / 'server.py'}")
    say("   启动后会自动打开控制台 http://127.0.0.1:8787")
    say(f"   （Windows 也可以双击 {ROOT / '启动服务.bat'} 或 后台启动.vbs）")
    say("   其中 `-B` 表示不写 .pyc：技能目录是要打包分发的，")
    say("   字节码缓存属于运行期产物，不该落在代码目录里。")
    say("\n2) 登录站点（必须，否则调不通）：")
    say("   在控制台「站点管理」里点「登录」，扫码登录一次即可，")
    say("   登录态长期有效。建议至少登录 DeepSeek + 豆包两家。")
    say("\n3) 接给智能体用：")
    say("   · HTTP：任何程序都能调 POST http://127.0.0.1:8787/api/ask")
    say("   · MCP：把下面这段加进 MCP 配置里")
    say("")
    # ★ 用 dict + json.dumps 生成，不要手写字符串拼 JSON。
    #   手写版塞注释、漏逗号都会变成**非法 JSON**，用户照抄进配置会
    #   把整份 MCP 配置弄坏（而且很难看出是复制来的片段的问题）。
    #   `-B` 同上：别让 MCP 宿主的解释器往技能目录里写 __pycache__。
    snippet = {
        "mcpServers": {
            "token-saver": {
                "command": make_json_path(py),
                "args": ["-B", make_json_path(ROOT / "mcp_bridge.py")],
            }
        }
    }
    for _line in json.dumps(snippet, ensure_ascii=False, indent=2).splitlines():
        say("   " + _line)
    say("")
    say("   注：`args` 里的 \"-B\" 可以删掉，只影响要不要在技能目录里生成")
    say("       __pycache__，删掉功能照常。")
    say("")
    say("   WorkBuddy 用户注意：加完配置要去「连接器管理」点一次「信任」才会生效。")
    say("")
    say(f"   附：运行数据会落在 {DATA}（登录态、浏览器 profile、用量统计）。")
    say("       想换位置就设环境变量 TOKEN_SAVER_HOME 再重跑本脚本。")
    say("\n更多说明见 references/setup.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
