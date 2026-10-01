"""MCP 桥接：让支持 MCP 的客户端（WorkBuddy / Claude Desktop 等）直接调用。

它本身不碰浏览器，只是把工具调用转发给正在运行的 Token Saver HTTP 服务。
所以必须先 `python server.py`，再把这个脚本挂进客户端的 MCP 配置。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

BASE = os.environ.get("TOKEN_SAVER_URL", "http://127.0.0.1:8787")
TIMEOUT = float(os.environ.get("TOKEN_SAVER_TIMEOUT", "200"))


def _post(path: str, payload: dict) -> dict:
    r = httpx.post(f"{BASE}{path}", json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _get(path: str) -> Any:
    r = httpx.get(f"{BASE}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


TOOLS: dict[str, dict] = {
    "ask_free_llm": {
        "description": (
            "把简单任务转派给本机网页端免费额度并取回答案。"
            "适用于翻译、摘要、润色改写、格式转换、术语解释、正则、单位换算等标准化任务。"
            "不适合多文件改造、架构设计、根因分析这类需要反复推理的任务。"
            "对同一话题继续追问时请带上上次返回的 thread，网页端会记得上文；"
            "不带 thread 则每次都是全新会话。"
            "需要配图或图片时，用 provider=doubao 发画图提示词，"
            "等 20~30 秒后再调 grab_images 把图取回本地。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "要外包出去的完整提示词"},
                "provider": {
                    "type": "string",
                    "description": "指定站点：deepseek/yuanbao/doubao/kimi/tongyi/gemini/chatgpt；auto 或不填=自动路由",
                },
                "thread": {
                    "type": "string",
                    "description": "话题 id。需要连续追问时传上一次调用返回的 thread，可让网页端保持上下文",
                },
                "reset": {
                    "type": "boolean",
                    "description": "true=强制重开会话。想换话题但复用同一 id 时用",
                },
                "no_mode": {
                    "type": "boolean",
                    "description": (
                        "true=不自动开深度思考/不切模型。**要豆包画图时必须传 true** —— "
                        "实测切到 2.1 Turbo 后它只回文字不画图。"
                    ),
                },
                "grab_images": {
                    "type": "boolean",
                    "description": (
                        "true=生图任务：回答完后再等图片渲染出来并下载到本地，"
                        "路径随结果返回。画图请务必带上它 —— 生成结果只活在当前"
                        "页面里，事后单独抓容易因为页面被关而拿不到。"
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    "fanout_ask": {
        "description": (
            "**并行**外包：把同一个任务拆成多个彼此独立的部分，同时丢给多家"
            "网页端 AI 做，最后按原顺序合并成一篇。适合长文档分块翻译/摘要、"
            "批量改写、多份素材各自提炼要点 —— 凡是拆开之后**互不依赖**的活。"
            "默认动态调度：谁先做完谁领下一段，总耗时从「各部分之和」压到"
            "接近「最慢那一家跑一段的时间」。**这就是理论下限** —— 再拆更多"
            "部分也不会更快，因为那家光跑一段就要这么久；想再快只能多登录"
            "一家快站点（纯 HTTP 直连的站点比开浏览器的快 10 倍以上）。"
            "注意：各部分不共享上文（每个都是全新会话），所以不适合"
            "「先问一句、再追问」这种连环任务，那种请用 ask_free_llm + thread。"
            "返回里带 speedup（相对串行的加速比）、各家实际领了几段、"
            "以及各部分的来源站点，便于核对。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "parts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "拆好的各部分内容，按最终的先后顺序给",
                },
                "template": {
                    "type": "string",
                    "description": (
                        "套在每个部分前面的指令模板，支持 {part} / {n} / {total} 占位符。"
                        "例：把下面这段翻译成中文，术语保持一致：\\n\\n{part}。"
                        "忘了写 {part} 也不会丢内容，会自动追加在末尾"
                    ),
                },
                "providers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "指定用哪几家做车道（如 [\"yuanbao\",\"doubao\",\"deepseek\"]）；不填则自动挑",
                },
                "merge": {
                    "type": "string",
                    "description": "合并方式：sections（默认，带小标题分隔）| concat（纯拼接）| json（不合并，只看 parts）",
                },
                "max_lanes": {
                    "type": "integer",
                    "description": "最多同时开几条车道（默认 4，每条=一个浏览器窗口）",
                },
                "schedule": {
                    "type": "string",
                    "description": (
                        "车道领活方式：dynamic（默认）谁先空谁领下一个，快的车道"
                        "自然多领、墙钟最短；roundrobin 开跑前平均分好，用于"
                        "刻意均摊、避免把某一家的额度打光"
                    ),
                },
                "retry_failed": {
                    "type": "boolean",
                    "description": "有部分失败时是否再串行补跑一次（默认 true）",
                },
            },
            "required": ["parts"],
        },
    },
    "stop_offload": {
        "description": (
            "立刻停止外包任务。批量转派任务进行到一半想中止时用 —— "
            "**关掉浏览器是停不住的**，自愈逻辑会重新把它打开。"
            "停机后后续调用会立即失败而不是再去开浏览器，"
            "正在等待的回答也会在秒级中断，不用等它跑完。"
            "要继续就调 resume_offload。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "停机原因，会写进返回给用户的提示里"},
            },
        },
    },
    "resume_offload": {
        "description": "解除 stop_offload 造成的停机，恢复正常调用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "analyze_task_offload": {
        "description": "在真正外包之前评估一下：这个任务值不值得丢给网页端。返回复杂度分与建议。",
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
    },
    "list_free_providers": {
        "description": "列出所有已接入的网页端站点、启用状态与浏览器是否已打开。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "close_windows": {
        "description": (
            "关闭网页端 AI 的浏览器窗口（不影响网关服务本身，下次调用会自动重开"
            "并复用登录态）。**用完不再需要追问时调它，把窗口收干净，别让用户手动关。**"
            "不确定还要不要继续追问就别调 —— 窗口闲置超过 idle_close_seconds"
            "（默认 180 秒）也会被自动关掉，托管给那个兜底更省事。"
            "默认关掉所有已打开的站点窗口；传 provider 只关某一家。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "只关这一家（deepseek/doubao/yuanbao/kimi…）；不填=全关",
                },
            },
        },
    },
    "savings_report": {
        "description": "查看省钱战绩：调用次数、省下 token、折合人民币、各站点分布。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "grab_images": {
        "description": (
            "从能生图的网页端（豆包等）抓取当前页面上最新生成的图片并下载到本地。"
            "典型用法：先用 ask_free_llm 指定 doubao 发提示词让它画图，"
            "等它画完再调本工具把图取回来展示（免费生图，省 token）。"
            "返回本地文件绝对路径，可直接作为交付物展示给用户。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "站点 id，生图请用 doubao",
                },
                "min_side": {
                    "type": "integer",
                    "description": "最小边长（像素），默认 512，用于排除头像和图标",
                },
                "wait_sec": {
                    "type": "integer",
                    "description": (
                        "最多等多少秒让图片渲染出来（默认 0=立即抓，建议 60）。"
                        "生图是异步的：文字回复先出来，图还要再画 10~40 秒。"
                    ),
                },
            },
            "required": ["provider"],
        },
    },
}


def call_tool(name: str, args: dict) -> list[dict]:
    try:
        if name == "ask_free_llm":
            r = _post("/api/ask", {
                "prompt": args.get("prompt", ""),
                "provider": args.get("provider") or None,
                "thread": args.get("thread") or None,
                "reset": args.get("reset"),
                "no_mode": bool(args.get("no_mode")),
                "grab_images": bool(args.get("grab_images")),
            })
            if not r.get("ok"):
                return [{"type": "text", "text": f"[失败] {r.get('error')}\n\n{r.get('hint','')}"}]
            th = f" · 话题 {r['thread']}" if r.get("thread") else ""
            cont = " · 续聊" if r.get("continued") else ""
            # 只报"确实开了"和异常情况，"未开/无此开关"不占位置
            mtxt = ""
            parts = []
            for k, v in (r.get("mode") or {}).items():
                nm = k[5:] if k.startswith("pick:") else k
                if v == "on":
                    parts.append(f"{nm}已开")
                elif v in ("unknown", "fail"):
                    parts.append(f"{nm}{'未知' if v == 'unknown' else '切换失败'}")
            if parts:
                mtxt = " · " + "/".join(parts)
            imgs = r.get("images") or []
            img_txt = ""
            if imgs:
                img_txt = ("\n\n[生成的图片已下载到本地，可直接作为交付物展示]\n"
                           + "\n".join(f"- {p}" for p in imgs))
            return [{"type": "text", "text":
                     f"[来源 {r['provider_name']}{th}{cont} · {r['elapsed']}s{mtxt} · "
                     f"约省 ¥{r['tokens']['saved_usd'] * 7.1:.4f}]\n\n"
                     f"{r['answer']}{img_txt}"}]

        if name == "fanout_ask":
            r = _post("/api/fanout", {
                "parts": args.get("parts") or [],
                "template": args.get("template") or "",
                "providers": args.get("providers") or None,
                "merge": args.get("merge") or "sections",
                "max_lanes": args.get("max_lanes"),
                "schedule": args.get("schedule") or "dynamic",
                "retry_failed": (args.get("retry_failed")
                                 if args.get("retry_failed") is not None else True),
            })
            if r.get("error") and not r.get("parts"):
                return [{"type": "text", "text": f"[失败] {r['error']}"}]
            head = (f"[并行 {r['parts_ok']}/{r['parts_total']} 部分成功 · "
                    f"车道 {'+'.join(r['lanes'])} · 墙钟 {r['wall_clock']}s")
            if r.get("sequential_estimate"):
                head += (f" · 串行约需 {r['sequential_estimate']}s"
                         f" · 加速 {r.get('speedup')}x")
            head += "]"
            # 各家实际领了几段 —— 动态调度下这是最直观的"谁快谁多干"证据，
            # 也是排查"某家是不是被饿死了"的第一手信息
            asg = r.get("assignment") or {}
            if asg:
                head += ("\n[分配] " + " · ".join(
                    f"{k} 领 {len(v)} 段" for k, v in asg.items()))
            bad = [p for p in r["parts"] if not p.get("ok")]
            warn = ""
            if bad:
                warn = "\n\n[有部分失败]\n" + "\n".join(
                    f"- 第{p['n']}部分（{p['provider']}）：{p['error']}" for p in bad)
            body = r.get("merged") or json.dumps(r["parts"], ensure_ascii=False, indent=2)
            return [{"type": "text", "text": f"{head}\n\n{body}{warn}"}]

        if name == "stop_offload":
            r = _post("/api/stop", {"reason": args.get("reason") or "通过 stop_offload 停机"})
            return [{"type": "text", "text": (
                "已停机。后续外包调用会立即失败，浏览器不会再被拉起；"
                "正在进行中的等待也已中断。\n"
                f"原因：{r.get('reason','')}\n"
                "恢复：调用 resume_offload，或 POST /api/resume；也可以直接删掉"
                " GET /api/status 返回的 stop_file 指向的那个哨兵文件（删掉即恢复）。")}]

        if name == "resume_offload":
            _post("/api/resume", {})
            return [{"type": "text", "text": "已恢复，可以正常调用外包了。"}]

        if name == "analyze_task_offload":
            r = _post("/api/analyze", {"prompt": args.get("prompt", "")})
            return [{"type": "text", "text": json.dumps(r, ensure_ascii=False, indent=2)}]

        if name == "list_free_providers":
            r = _get("/api/providers")
            lines = [f"- {p['id']}: {p['name']} | 启用={p['enabled']} | "
                     f"浏览器已开={p['running']} | 标签={','.join(p['tags'])}"
                     for p in r]
            return [{"type": "text", "text": "\n".join(lines)}]

        if name == "grab_images":
            r = _post("/api/images", {
                "provider": args.get("provider") or "doubao",
                "min_side": args.get("min_side") or 512,
                "wait_sec": args.get("wait_sec") or 0,
            })
            if not r.get("count"):
                return [{"type": "text", "text":
                         "页面上没有找到够大的生成图片。若是刚发完提示词，"
                         "带上 wait_sec=60 再调一次（生图比文字慢 10~40 秒）。"}]
            lines = [f"抓到 {r['count']} 张图，已下载到本地："]
            lines += [f"- {p}" for p in r["files"]]
            lines.append("\n直接把这些路径作为交付物展示即可。")
            return [{"type": "text", "text": "\n".join(lines)}]

        if name == "close_windows":
            r = _post("/api/close", {"provider": args.get("provider") or None})
            closed = r.get("closed") or []
            if not closed:
                return [{"type": "text", "text":
                         "当前没有开着的窗口，无需关闭。（网关仍在运行）"}]
            return [{"type": "text", "text":
                     f"已关闭 {len(closed)} 个窗口：{', '.join(closed)}。\n"
                     "网关仍在运行，下次调用会自动重开并复用登录态"
                     "（首次重开约多花几秒）。"}]

        if name == "savings_report":
            r = _get("/api/stats")
            return [{"type": "text", "text": (
                f"外包 {r['calls']} 次，成功 {r['ok']} 次（{r['success_rate']}%）\n"
                f"累计省下 token：{r['tokens_saved']:,}\n"
                f"折合人民币：¥{r['cny_saved']}（对照 {r['reference_model']}）\n"
                f"平均耗时：{r['avg_elapsed']}s\n"
                f"各站点：{json.dumps(r['by_provider'], ensure_ascii=False)}"
            )}]

        return [{"type": "text", "text": f"未知工具 {name}"}]
    except httpx.HTTPError as e:
        return [{"type": "text",
                 "text": f"调用失败：{e}。请确认 Token Saver 服务已启动（{BASE}）。"}]


# ------------------------------------------------------------------ MCP 服务
def _server_cls():
    """兼容 mcp 1.x / 2.x：FastMCP 在 2.x 改名为 MCPServer。"""
    try:
        from mcp.server.fastmcp import FastMCP
        return FastMCP
    except Exception:  # noqa: BLE001
        from mcp.server.mcpserver import MCPServer
        return MCPServer


def main() -> None:
    try:
        cls = _server_cls()
    except Exception as e:  # noqa: BLE001
        print(f"缺少 mcp 依赖：{e}\n请 pip install mcp", file=sys.stderr)
        raise SystemExit(1)

    mcp = cls("token-saver")

    @mcp.tool(name="ask_free_llm", description=TOOLS["ask_free_llm"]["description"])
    async def ask_free_llm(prompt: str, provider: str = "auto",
                           thread: str = "", reset: bool = False,
                           no_mode: bool = False,
                           grab_images: bool = False) -> str:
        return call_tool("ask_free_llm", {
            "prompt": prompt, "provider": provider,
            "thread": thread, "reset": reset or None,
            "no_mode": no_mode, "grab_images": grab_images,
        })[0]["text"]

    @mcp.tool(name="fanout_ask", description=TOOLS["fanout_ask"]["description"])
    async def fanout_ask(parts: list[str], template: str = "",
                         providers: list[str] | None = None,
                         merge: str = "sections",
                         max_lanes: int = 0) -> str:
        return call_tool("fanout_ask", {
            "parts": parts, "template": template, "providers": providers,
            "merge": merge, "max_lanes": max_lanes or None,
        })[0]["text"]

    @mcp.tool(name="stop_offload", description=TOOLS["stop_offload"]["description"])
    async def stop_offload(reason: str = "") -> str:
        return call_tool("stop_offload", {"reason": reason})[0]["text"]

    @mcp.tool(name="resume_offload", description=TOOLS["resume_offload"]["description"])
    async def resume_offload() -> str:
        return call_tool("resume_offload", {})[0]["text"]

    @mcp.tool(name="analyze_task_offload", description=TOOLS["analyze_task_offload"]["description"])
    async def analyze_task_offload(prompt: str) -> str:
        return call_tool("analyze_task_offload", {"prompt": prompt})[0]["text"]

    @mcp.tool(name="list_free_providers", description=TOOLS["list_free_providers"]["description"])
    async def list_free_providers() -> str:
        return call_tool("list_free_providers", {})[0]["text"]

    @mcp.tool(name="savings_report", description=TOOLS["savings_report"]["description"])
    async def savings_report() -> str:
        return call_tool("savings_report", {})[0]["text"]

    @mcp.tool(name="grab_images", description=TOOLS["grab_images"]["description"])
    async def grab_images(provider: str = "doubao", min_side: int = 512,
                          wait_sec: int = 0) -> str:
        return call_tool("grab_images", {
            "provider": provider, "min_side": min_side,
            "wait_sec": wait_sec})[0]["text"]

    @mcp.tool(name="close_windows", description=TOOLS["close_windows"]["description"])
    async def close_windows(provider: str = "") -> str:
        return call_tool("close_windows", {"provider": provider})[0]["text"]

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
