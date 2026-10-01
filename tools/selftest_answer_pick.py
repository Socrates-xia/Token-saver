"""离线回归测试：答案元素选择（PICK_ANSWER_JS）+ 排除规则防呆。

    python tools/selftest_answer_pick.py

不联网、不消耗账号额度；本机会开一个 headless Edge 来跑 DOM 用例。
（用 msedge 而不是 chromium：本机装了 Edge，省掉 150MB 的浏览器下载。）

锁定四件真实踩过的事故（2026-10-01）：

1. **答案被腰斩**：`div[class*='markdown']` 会同时命中外层答案容器和它内部的
   每一个子块（元宝的表格包裹层 hyc-common-markdown__table-wrapper）。
   老代码取"最后一个命中"，于是只捞回最后一张表（158 字），
   前面整段"核心要点"静默丢失 —— 不报错、不告警。
   → 必须取"最外层候选里最后一个"。

2. **CSS4 复杂否定静默失效**：元宝原来用
   `div[class*='markdown']:not(.hyc-component-deepsearch-cot *)` 排思考面板。
   Chromium 实测该选择器命中 **0** 个元素且不报错 → 首选选择器整个失效，
   悄悄落到泛选择器上，思考过程照样被抓回来。
   → 源码契约：禁止在 selectors.answer 里用 `:not(<含空格的后代选择器>)`。

3. **排除范围搞错 → 等到超时**：元宝的 .hyc-component-deepsearch-cot 是
   **整条 AI 消息的外壳**（思考块 + 正文都是它的子块）。
   一开始按"思考面板"把它整个排掉，结果 snapshot_answer() 永远返回空，
   一路等到 ask_timeout（240s）再退到别的站点重来，日志上只看到一句"超时"。
   → 必须排除 __think 这一层；并且要有防呆，30 秒就主动报错。

4. **回显判定被自己的回填块关掉**（豆包生图轮）：回填给网页端的历史块里
   曾写着"用户问：…"，而 `_REASONING_MARKERS` 里也含"用户问"（那张表
   本是给"深度思考在复述对话"用的豁免名单）。两处文案一撞，
   `looks_like_echo` 的豁免分支就先返回 False —— 回显判定整个失效，
   我方发出去的那段文本被当成答案收下（3618 字），还写进话题历史、
   下一轮又回填下去，越滚越大（6173 字）。
   → 铁证（逐字相同 / 以原提问开头）必须排在豁免**之前**；
     回填块的文案也得换词。两条都锁在本文件里。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.adapter import PICK_ANSWER_JS  # noqa: E402
from core.adapter_yuanbao import Yuanbao  # noqa: E402

fails: list[str] = []
total = 0


def check(name: str, got, want) -> None:
    global total
    total += 1
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {name}")
    if not ok:
        print(f"      期望 {want!r}")
        print(f"      实际 {got!r}")
        fails.append(name)


def check_true(name: str, got) -> None:
    check(name, bool(got), True)


# ------------------------------------------------------------------ DOM 素材
# 复刻元宝真实结构（2026-10-01 /api/debug/dom?kind=sel 抓下来的）：
#   外壳 .hyc-component-deepsearch-cot 里有两个平级的子块 —— 思考 和 正文，
#   正文里又套了一层表格包裹层。这正是让老代码翻车的那个形状。
THINK_TEXT = "用户要求用表格对比番茄工作法和时间盒。我需要先明确两者的定义，" \
             "然后从六个维度展开：时间单位、任务拆分方式、中断处理、复盘方式、" \
             "灵活性、适用人群。番茄工作法强调固定节奏，时间盒强调任务边界……"
ANSWER_HEAD = "以下是番茄工作法（Pomodoro Technique）与时间盒（Timeboxing）的区别对比："
ANSWER_TAIL = "总结：两者可以结合使用 —— 用时间盒规划大块工作和截止时间，" \
              "用番茄钟保证执行时的专注与休息节奏。"

# 元宝塞进答案里的推荐视频卡片。★ 它和正文**同属一个** .hyc-common-markdown
# 容器（层级 .hyc-common-markdown > .ybc-p > ...bigCard-wrapper），
# 所以换选择器躲不开，只能靠 answer_strip 从克隆体里摘掉。
VIDEO_CARD = "相关视频00:44每天一分钟舒尔特方格练习#专注力#舒尔特训练"

PAGE_HTML = f"""
<div id="app">
  <div class="hyc-component-deepsearch-cot">
    <div class="hyc-component-deepsearch-cot__think hyc-component-deepsearch-cot__think--inline">
      <div class="hyc-component-deepsearch-cot__think__header-container">
        <div class="hyc-component-deepsearch-cot__think__header">
          <span>已深度思考(用时11秒)</span>
        </div>
      </div>
      <div class="hyc-component-deepsearch-cot__think__content">
        <div class="hyc-content-md hyc-content-md-done">
          <div class="hyc-common-markdown">{THINK_TEXT}</div>
        </div>
      </div>
    </div>
    <div class="hyc-common-markdown hyc-common-markdown-style hyc-common-markdown-style-cot">
      <p>{ANSWER_HEAD}</p>
      <div class="hyc-common-markdown__table-wrapper" data-has-scroll="false">
        <table>
          <tr><th>对比维度</th><th>番茄工作法</th><th>时间盒</th></tr>
          <tr><td>时间单位</td><td>25 分钟</td><td>自定 60~120 分钟</td></tr>
          <tr><td>任务拆分</td><td>按番茄钟切分</td><td>按交付边界切分</td></tr>
        </table>
      </div>
      <p>{ANSWER_TAIL}</p>
      <div class="ybc-p">
        <div class="ybc-chat-videoBoxV2-bigCard-wrapper video-box-v2_ybc-chat-videoBoxV2-bigCard">
          <div class="ybc-chat-videoBoxV2-bigCard__title">{VIDEO_CARD}</div>
        </div>
      </div>
    </div>
  </div>
</div>
"""

SEL = "div[class*='hyc-common-markdown']"
# 与 core/adapter_yuanbao.py 的 answer_strip 保持一致
STRIP = ["[class*='videoBox']", "[class*='video-box']",
         "[class*='relatedQuestion' i]"]


def check_js_syntax() -> None:
    """把内嵌的 JS 常量丢给 node --check 过一遍语法。

    ★ 这是本仓库最值得保留的一条测试。
    教训：TABLE_TO_MD_JS 里有一句注释写成了 `"减号 + 反斜杠n + 数字"`，
    在普通（非 raw）三引号字符串里那个 \\n 会被 Python 先解释成真换行，
    把 // 注释劈成两行、露出没被注释掉的残句 → 整个 JS 变成
    SyntaxError: Invalid or unexpected token。
    而调用方 `except Exception: 退回 inner_text` 把它吞了，
    于是"表格没转 Markdown、公式没还原、角标没清"整整三项功能静默失效。
    Python 侧语法完全正确（字符串本身合法），任何 Python 检查都发现不了 ——
    只有真的拿一个 JS 引擎解析它才行。
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        # 本机托管 node 的固定位置（见项目 README 的运行时约定）
        for c in (Path.home() / ".workbuddy/binaries/node/versions"
                  / "22.12.0" / "node.exe",
                  Path("C:/Program Files/nodejs/node.exe")):
            if c.exists():
                node = str(c)
                break
    if not node:
        print("  ⚠ 找不到 node，跳过 JS 语法检查（强烈建议装上）")
        return

    from core.adapter import TABLE_TO_MD_JS, _TO_MD_JS, PICK_ANSWER_JS
    # _TO_MD_JS 是**语句片段**（只声明了 toMD，不是表达式），
    # 得包进函数体里才能解析；另两个本身就是箭头函数表达式。
    cases = {
        "_TO_MD_JS": ("function __check__() {\n" + _TO_MD_JS + "\n}\n", True),
        "TABLE_TO_MD_JS": ("const f = " + TABLE_TO_MD_JS.strip() + ";\n", False),
        "PICK_ANSWER_JS": ("const f = " + PICK_ANSWER_JS.strip() + ";\n", False),
    }
    for name, (src, _is_snippet) in cases.items():
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(src)
            tmp = f.name
        try:
            p = subprocess.run([node, "--check", tmp],
                               capture_output=True, text=True, timeout=30)
            ok = p.returncode == 0
            check(f"{name} 能被 JS 引擎解析", ok, True)
            if not ok:
                print("      " + (p.stderr or "").strip().splitlines()[0])
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def main() -> None:
    print("=== 源码契约（不需要浏览器）===")
    yb_src = (ROOT / "core" / "adapter_yuanbao.py").read_text(encoding="utf-8")
    ad_src = (ROOT / "core" / "adapter.py").read_text(encoding="utf-8")

    # 契约 1：元宝排的是 __think 这一层，不是整条消息的外壳
    y = Yuanbao()
    check("元宝排除的是 __think 层（不是整条消息的外壳）",
          y.answer_exclude, [".hyc-component-deepsearch-cot__think"])

    # 契约 2：不允许在答案选择器里出现 CSS4 复杂否定（本机 Chromium 静默失效）
    bad = [s for s in y.selectors.answer
           if re.search(r":not\(\s*[^)]*\s[^)]*\)", s)]
    check("答案选择器里没有 CSS4 复杂否定 :not(.x *)", bad, [])

    # 契约 3：元宝必须摘掉推荐视频卡片，且和 DOM 用例用的是同一份名单
    check("元宝配了 answer_strip（摘推荐卡片）", bool(y.answer_strip), True)
    check("answer_strip 与 DOM 用例的名单一致", list(y.answer_strip), STRIP)
    check_true("answer_strip 覆盖视频卡片类名",
               any("videoBox" in s for s in y.answer_strip))

    # 契约 4：防呆存在且在两个轮询循环里都被调用
    check_true("存在 _check_exclude_sanity 防呆",
               "def _check_exclude_sanity" in ad_src)
    check("防呆在两个轮询循环里都被调用", ad_src.count("self._check_exclude_sanity()"), 2)

    print()
    print("=== JS 语法守门（有 node 就跑）===")
    check_js_syntax()

    print()
    print("=== 防呆逻辑（纯 Python）===")
    from core.adapter_deepseek import DeepSeek
    d = DeepSeek()
    d._excluded_since = time.time() - 1          # 才刚开始排除 → 不该报错
    try:
        d._check_exclude_sanity()
        check("排除刚开始 → 不报错", True, True)
    except RuntimeError:
        check("排除刚开始 → 不报错", False, True)

    d._excluded_since = time.time() - 31         # 连坐 31 秒 → 必须报错
    try:
        d._check_exclude_sanity()
        check("排除连坐 31 秒 → 主动报错", False, True)
    except RuntimeError as e:
        check_true("排除连坐 31 秒 → 主动报错", "排除了" in str(e))

    d._excluded_since = 0.0                      # 正常态 → 不该报错
    try:
        d._check_exclude_sanity()
        check("正常态 → 不报错", True, True)
    except RuntimeError:
        check("正常态 → 不报错", False, True)

    print()
    print("=== 回显判定（looks_like_echo，纯 Python）===")
    from core.adapter import looks_like_echo
    from core.pool import _build_context

    # ★ 真机事故 2026-10-01（豆包生图）：回填历史的那一刻，我方发出去的整段
    #   文本被当成"答案"抓了回来。根因是回填块里的"用户问："撞上了
    #   _REASONING_MARKERS，把 looks_like_echo 的豁免分支整个关掉了。
    hist_ctx = _build_context(
        [{"q": "请系统地列出中国的所有主要节日，包括传统节日和法定节假日。",
          "a": "以下系统梳理中国的主要节日，分为四大类。"}])
    sent = hist_ctx + "画一只奶龙，卡通风格"
    check("★ 回填过的请求抓回自己发出去的整段文本 → 判回显",
          looks_like_echo(sent, sent), True)
    check("抓回的容器以原提问开头（把用户消息也包进去）→ 判回显",
          looks_like_echo(sent + "答：我会画一只奶龙。", sent), True)
    check("正常短回答 → 不判回显",
          looks_like_echo("我会把它处理成极简线条：橘色斑纹、圆脸。",
                          "画一只橘猫，卡通风格的简笔画，要可爱一点"), False)
    check("深度思考的推理过程（含'用户问'）→ 不判回显，别误伤",
          looks_like_echo("让我想想。用户问的是永城天气，我需要先查资料。"
                          "永城今天阴天，13到19度。", "永城今天天气怎么样？"),
          False)

    print()
    print("=== DOM 用例（headless Edge）===")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠ 没装 playwright，跳过 DOM 用例")
        report()
        return

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ 起不了 Edge（{type(e).__name__}），跳过 DOM 用例")
            report()
            return
        page = browser.new_page()
        page.set_content(PAGE_HTML, wait_until="load")

        think = ".hyc-component-deepsearch-cot__think"

        # --- 正确配置：排掉 __think + 摘掉推荐卡片 ---
        r = page.evaluate(PICK_ANSWER_JS, [SEL, [think], STRIP, 60000])
        txt = (r or {}).get("text", "")
        check_true("排掉 __think → 拿得到答案", txt)
        check_true("答案里含正文开头", ANSWER_HEAD in txt)
        check_true("答案里含结论段（★ 老代码丢的就是这一段）", ANSWER_TAIL in txt)
        check_true("答案里含表格 Markdown 表头", "| 对比维度 | 番茄工作法 | 时间盒 |" in txt)
        check_true("答案里不含思考过程", "我需要先明确两者的定义" not in txt)
        check("嵌套的表格包裹层被过滤掉（否会被腰斩）",
              r.get("nested_dropped"), 1)

        # --- 推荐卡片：和正文同容器，只能靠 answer_strip 摘掉 ---
        check_true("推荐视频卡片被摘掉（answer_strip 生效）", "相关视频" not in txt)
        check_true("卡片摘掉后正文没被误伤", ANSWER_TAIL in txt)
        no_strip = page.evaluate(PICK_ANSWER_JS, [SEL, [think], [], 60000])
        check_true("不摘的话卡片会混进答案（模拟原状）",
                   "相关视频" in (no_strip or {}).get("text", ""))

        # --- 老代码的行为：无排除时取"最后一个命中"会拿到内层块 ---
        bare = page.evaluate(PICK_ANSWER_JS, [SEL, [], STRIP, 60000])
        check_true("不排除时答案仍是完整正文（最外层过滤生效）",
                   ANSWER_TAIL in (bare or {}).get("text", ""))

        # --- 踩过的坑：把整条消息外壳当思考面板排掉 → 全空 + excluded>0 ---
        wrong = page.evaluate(PICK_ANSWER_JS,
                              [SEL, [".hyc-component-deepsearch-cot"], STRIP, 60000])
        check("排掉整壳 → 抓不到答案（模拟事故）", (wrong or {}).get("text"), "")
        check_true("排掉整壳 → 报告 excluded>0（防呆的依据）",
                   (wrong or {}).get("excluded", 0) > 0)

        browser.close()

    report()


def report() -> None:
    print()
    if fails:
        print(f"✗ {len(fails)}/{total} 项失败：{'、'.join(fails)}")
        raise SystemExit(1)
    print(f"✓ 全部通过（{total} 项）")


if __name__ == "__main__":
    main()
