"""网页端聊天适配器基类。

设计原则：**通用启发式优先，厂商选择器只做精化**。
各家前端三天两头改版，若死盯某个 class 必然频繁崩。因此这里把
「定位输入框 / 等待生成结束 / 抓答案」拆成多级策略：

    Level 1  厂商自定义选择器（快、准，可能失效）
    Level 2  ARIA 角色 / data-* 语义（较稳）
    Level 3  纯几何启发式（最大可见输入框、页面右侧最新文本块）
    Level 4  点击"复制回答"按钮 → 读剪贴板（最稳，几乎只依赖按钮文案）

任一级拿到结果就停。这样即使某家改版，也极少出现整体不可用。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from playwright.async_api import Page

# ---------------------------------------------------------------- 通用常量

GENERIC_INPUT_SELECTORS: list[str] = [
    "textarea",
    "div[contenteditable='true']",
    "[role='textbox']",
    "div[contenteditable='plaintext-only']",
]

# 答案容器：从"最可能"到"最泛"
GENERIC_ANSWER_SELECTORS: list[str] = [
    "[data-message-author-role='assistant']",
    "[data-role='assistant']",
    ".markdown-body",
    "[class*='markdown-body']",
    "[class*='MarkdownBody']",
    "[class*='markdown']",
    "[class*='message-content']",
    "[class*='MessageContent']",
    "[class*='answer'] .text",
    "article",
]

SEND_BUTTON_HINTS: list[str] = [
    "[data-testid*='send']",
    "button[aria-label*='发送' i]",
    "button[aria-label*='Send' i]",
    "button[aria-label*='send' i]",
    "button[data-testid='send-button']",
    "[class*='send-button']",
    "button[type='submit']",
]

STOP_HINTS: list[str] = [
    "[data-testid*='stop']",
    "button[aria-label*='停止' i]",
    "button[aria-label*='Stop' i]",
    "button[aria-label*='stop' i]",
    "[class*='stop-button']",
]

# 流式指示器：就算页面没有"停止"按钮，也能靠这些判断还在生成
STREAMING_HINTS: list[str] = [
    "[class*='streaming' i]",
    "[class*='typing' i]",
    "[class*='thinking' i]",
    "[class*='generating' i]",
    "[data-testid*='thinking']",
    "[data-testid*='reasoning']",
]

# 页面上出现这些文字 = 还在想 / 还在写
# 注意：只放"进行时"词。像"已深度思考（用时N秒）""思考完毕"这种完成态
# 会在生成结束后一直留在页面上，放进来会让等待逻辑永远退不出去。
THINKING_TEXTS: list[str] = [
    "思考中", "正在思考", "正在生成", "生成中", "正在输入",
    "thinking…", " thinking", "please wait",
    "正在联网", "正在搜索", "正在阅读",
    # Agent 模式：还在翻网页，不能收工
    "正在浏览", "浏览网页", "搜索中", "已搜索", "正在打开",
]

NEW_CHAT_HINTS: list[str] = [
    "[aria-label*='新对话' i]",
    "[aria-label*='新建对话' i]",
    "[aria-label*='New chat' i]",
    "[aria-label*='New Chat' i]",
    "[data-testid*='new-chat']",
]

COPY_BUTTON_HINTS: list[str] = [
    "button[aria-label*='复制' i]",
    "button[aria-label*='Copy' i]",
    "[data-testid*='copy' i]",
    "[class*='copy-button' i]",
    "[class*='copy-btn' i]",
    "[class*='copyButton' i]",
    # 刻意不放泛泛的 [class*='copy']：像 DeepSeek 那种纯图标按钮组里，
    # 候选里会混进"重新生成"，点错就等于让模型重写一遍答案。宁可走 DOM 路径。
]

LOGGED_IN_MARKERS: list[str] = [
    "textarea",
    "div[contenteditable='true']",
    "[role='textbox']",
]

LOGGED_OUT_MARKERS: list[str] = [
    "text=登录",
    "text=注册",
    "text=扫码",
    "text=Log in",
    "text=Sign in",
    "text=Sign up",
]

# ---------------------------------------------------------------- 模式开关
# 「深度思考 / 联网搜索 / Think longer」这类开关，各家实现五花八门：
#   DeepSeek   div.ds-toggle-button + aria-pressed
#   元宝/豆包  button + class 变体（有的带 aria-pressed，有的没有）
#   通义       button + data-state
#   ChatGPT    "Think longer"，部分版本 role=switch
# 所以状态不能只看 aria-pressed，得按优先级一路试下来。
# ------------------------------------------------------------ 弹窗拦截层
# 各家前端进场就爱弹东西：更新日志、功能介绍、满意度调查、Cookie 同意、
# "深度思考已上线"之类。这些浮层会**遮住输入框和模式开关**，导致后续点击
# 全部超时（Playwright 会一直等到元素可点为止）。所以每一步操作之前都得先清场。

# 弹窗容器：命中其一才认为"现在有浮层"
POPUP_ROOTS: list[str] = [
    "[role='dialog']",
    "[aria-modal='true']",
    "[class*='modal' i]",
    "[class*='dialog' i]",
    "[class*='popup' i]",
    "[class*='drawer' i]",
    "[class*='overlay' i]",
    "[class*='update-log' i]",
    "[class*='announcement' i]",
    "[class*='feedback-modal' i]",
]

# 关闭按钮的文案 / aria-label 关键词。命中才点，
# 绝不靠 class 名猜 —— 猜错就会把页面上的正常按钮点掉。
POPUP_CLOSE_WORDS: list[str] = [
    "关闭", "关掉", "我知道了", "我知道啦", "不再提示", "稍后再说", "稍后",
    "取消", "跳过", "暂不", "以后再说", "直接体验", "立即体验",
    "close", "dismiss", "cancel", "got it", "no thanks", "not now",
    "skip", "later", "ok", "i agree", "accept all", "reject all",
]

# 全站通用的关闭键候选，按可靠性排序
GENERIC_POPUP_CLOSE: list[str] = [
    "[class*='modal' i] [class*='close' i]",
    "[class*='modal' i] [aria-label*='close' i]",
    "[role='dialog'] [class*='close' i]",
    "[role='dialog'] [aria-label*='close' i]",
    "[class*='overlay' i] [class*='close' i]",
    "[aria-label*='关闭' i]",
    "[aria-label*='Close' i]",
    "[title*='关闭' i]",
]

TOGGLE_STATE_ATTRS: list[str] = [
    "aria-pressed", "aria-checked", "aria-selected",
    "data-state", "data-active", "data-checked", "data-enabled", "data-on",
]

# 这些值一律视为"开"。注意 'on' 同时也是 class 命中词，故要合并处理。
TOGGLE_TRUE_VALUES: set[str] = {
    "true", "1", "on", "checked", "active", "selected", "enabled", "yes",
}

# 属性都读不到时，退到 class 关键字（弱信号，只作兜底）
TOGGLE_ON_CLASS_RE = r"(^|[\s_-])(active|selected|on|checked|enabled)([\s_-]|$)"


@dataclass
class Selectors:
    """一组可覆盖的选择器候选。列表按优先级排列。"""
    input: list[str] = field(default_factory=list)
    send_button: list[str] = field(default_factory=list)
    stop_button: list[str] = field(default_factory=list)
    answer: list[str] = field(default_factory=list)
    copy_button: list[str] = field(default_factory=list)
    new_chat: list[str] = field(default_factory=list)
    # 该站特有的"关闭弹窗"按钮选择器，优先级高于通用启发式。
    # 站点改版后在 config.yaml 的 providers.<id>.selectors.popup_close 里追加即可，
    # 不用改代码。
    popup_close: list[str] = field(default_factory=list)


# 把答案里的 <table> 反向还原成 Markdown 表格，否则纯文本抓取会把表格拉散。
#
# ★★ 这里是全项目最贵的一个坑，务必读完再改（2026-10-01）★★
#
# 下面这段 JS 必须用**原始字符串**（r"""）包起来。
#
# 起因：脚本里原本有一句注释，举例说明"短横和数字之间夹着换行"这个形态，
# 写成了英文双引号包着的 减号 + 反斜杠n + 数字。在**普通**三引号字符串里，
# 那个 \n 会被 Python 先解释成一个真换行 —— 于是 // 注释被劈成两行，
# 第二行开头露出一段没被注释掉的文本，整个 JS 直接
#     SyntaxError: Invalid or unexpected token
# 更麻烦的是它**不报错**：调用方当时写的是
#     try: txt = await el.evaluate(TABLE_TO_MD_JS)
#     except Exception: txt = await el.inner_text()      # ← 静默降级
# 于是"表格转 Markdown / 数学公式还原成 TeX / 在 DOM 层清掉引用角标"
# 这三件事**从来没有生效过**，全线静默退化成了 innerText。没人发现，
# 因为退化后的结果看起来"也像那么回事"，只是表格被拉成了一行行文字。
#
# 所以修的时候是两件事一起改的：
#   1) 这段 JS 改用 r"""（本段），里面的 \n / \| 原样交给 JS；
#   2) 调用方不再静默吞异常（见 snapshot_answer / extract 里的 record）。
# 只做第 1 件，下次换个写法还会重演；只做第 2 件，故障至少能立刻暴露。
_TO_MD_JS = r"""
  // 把表格转成 Markdown；同时把公式还原成 TeX 源码，并摘掉引用角标和站点塞的附加块。
  const toMD = (el, strips) => {
    const rowsMD = (t) => {
      const lines = [];
      Array.from(t.querySelectorAll('tr')).forEach((tr, ri) => {
        const cells = Array.from(tr.children)
          .map(c => (c.innerText || '').trim()
                     .replace(/\|/g, '\\|').replace(/\n/g, ' '));
        if (!cells.length) return;
        lines.push('| ' + cells.join(' | ') + ' |');
        if (ri === 0 && tr.querySelector('th')) {
          lines.push('| ' + cells.map(() => '---').join(' | ') + ' |');
        }
      });
      return lines.join('\n');
    };
    const clone = el.cloneNode(true);
    // ★ 站点塞进答案里的**非答案内容**必须在这里摘掉（见 answer_strip）。
    //   最容易漏的是"推荐卡片"：元宝会把推荐视频放在**同一个**
    //   .hyc-common-markdown 容器里（结构是
    //   .hyc-common-markdown > .ybc-p > .ybc-chat-videoBoxV2-bigCard-wrapper），
    //   所以它和正文是同一个元素的文本，靠"换一个容器"根本躲不开 ——
    //   实测答案尾巴被挂上"相关视频00:44……艾袒心ADHD成长营2周前"。
    //   注意这些块只存在于**页面上**，站点"复制"按钮给的 Markdown 原文里没有，
    //   所以走剪贴板那条路时不受影响。
    if (strips && strips.length) {
      try {
        clone.querySelectorAll(strips.join(',')).forEach(n => n.remove());
      } catch (_) {}
    }
    // 公式：渲染后的 innerText 会把上下标拉平成一坨乱码
    // （"3.3 V212=3.34096"），但渲染引擎会在 MathML 里保留 TeX 源码，取它。
    clone.querySelectorAll('mjx-container, .katex, [class*="mathjax"], math')
      .forEach(m => {
        const ann = m.querySelector('annotation[encoding="application/x-tex"]')
                 || m.querySelector('annotation');
        const tex = ann && (ann.textContent || '').trim();
        if (tex) m.replaceWith(document.createTextNode(' $' + tex + '$ '));
      });
    // 引用角标必须在**这里**（DOM 层）摘掉，不能只靠后面的文本正则：
    //   · DeepSeek 的角标是 span.ds-markdown-cite，不是 sup/sub，
    //     传统的 sup 清理对它无效；
    //   · 它渲染出来的文字是"短横 + 换行 + 数字"的形态，
    //     innerText 拿到的是 "- 20" 这种夹空格的字符串，
    //     纯文本规则很难既清干净又不误伤日期（2023-2024）
    //     和型号（STM32F103）。实测只靠正则会漏掉
    //     "升学率达21.03%-11"这种紧跟在百分号后面的角标。
    clone.querySelectorAll(
      'sup, sub, [class*="citation" i], [class*="reference" i], [class*="footnote" i],'
      + ' [class*="markdown-cite" i], [class*="cite" i]'
    ).forEach(n => n.remove());
    clone.querySelectorAll('table').forEach(t => {
      const md = rowsMD(t);
      if (md) {
        const node = document.createElement('pre');
        node.textContent = '\n' + md + '\n';
        t.replaceWith(node);
      }
    });
    return clone.innerText;
  };
"""

# 对"指定元素"取带表格 Markdown 的文本。
# 对"指定元素"取带表格 Markdown 的文本。
# 第二参数是"要摘掉的子树"选择器；这个入口没有站点上下文，所以传空。
TABLE_TO_MD_JS = "(el) => {" + _TO_MD_JS + "\n  return toMD(el, []);\n}\n"

# 答案抽取：一个选择器拿到的是**一组**命中元素（外层容器 + 它的每一块子节点）。
# 返回"最外层候选里最后一个"的文本。
#
# ★ 为什么要做"最外层"过滤（2026-10-01 实测）：
#   元宝回答里含表格时，[class*='markdown'] 会命中 8 个元素 ——
#   思考面板、答案容器、以及答案内部的表格包裹层
#   （hyc-common-markdown__table-wrapper）。
#   老代码无脑取"最后一个命中"，于是拿到的是**表格包裹层**（792 字），
#   答案前半段（核心要点那一整节）被静默丢掉 —— 不报错、不告警，
#   用户只会觉得"这答案怎么有点短"。
#   更糟的是流式过程中表格单元格先是省略号占位，这段文字能连续稳定好几秒，
#   而"文本稳定 N 秒即完成"正是判完成的依据 → 当场收工，
#   只捞回一行表头（实测 66 字）。
#
# 只取"最外层"对**其他站点是零行为变化**：如果最后一个命中本来就是顶层
# （DeepSeek / 豆包 / Kimi 都是这种结构），tops 的末位就是它本身。
# 只有末位是被包住的子块时结果才会变 —— 而那正是要修的场景。
PICK_ANSWER_JS = "([sel, excludes, strips, maxLen]) => {" + _TO_MD_JS + r"""
  let els = [];
  try { els = Array.from(document.querySelectorAll(sel)); }
  catch (e) { return {text: "", sel: sel, raw: -1, total: 0,
                      excluded: 0, nested_dropped: 0, badSelector: true}; }

  // ---- 可见 + 排除（思考面板等）----
  const vis = [];
  for (const e of els) {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    let drop = false;
    for (const ex of excludes) {
      try { if (e.closest(ex)) { drop = true; break; } } catch (_) {}
    }
    if (drop) continue;
    vis.push(e);
  }
  if (!vis.length) {
    // 空手而归也要说清"是没有候选"还是"候选全被 answer_exclude 排掉了"。
    // 两者外行看起来一样（都是抓不到答案），但后者是配置写错，
    // 必须能一眼分辨，否则只能干等到超时。
    return {text: "", sel: sel, raw: els.length, total: 0,
            excluded: els.length - vis.length, nested_dropped: 0};
  }

  const tops = vis.filter(e => !vis.some(o => o !== e && o.contains(e)));
  const pool = tops.length ? tops : vis;

  // ★ 长度下限必须是 1 而不是 2：像"2"这种单字符答案完全合法，
  //   卡在 >=2 会把它筛掉、转而取到上一层的思考过程 ——
  //   而思考过程里往往重复着用户的提问，又会被"回声检测"判成提问回显，
  //   最后空转超时（实测白等 90 秒）。
  for (let i = pool.length - 1; i >= 0; i--) {
    let txt = '';
    try { txt = (toMD(pool[i], strips) || '').trim(); }
    catch (_) { txt = (pool[i].innerText || '').trim(); }
    if (txt.length >= 1 && txt.length < maxLen) {
      return {text: txt, sel: sel, idx: i, total: pool.length,
              raw: els.length, nested_dropped: vis.length - pool.length,
              excluded: 0};
    }
  }
  return {text: "", sel: sel, raw: els.length, total: pool.length,
          excluded: els.length - vis.length, nested_dropped: 0};
}
"""
# 开启联网/深度思考后，部分站点会进入"Agent 多步模式"：先输出一段检索计划
# （"并行搜索已完成，现在需要打开相关页面…"），过很久才给最终答案。
# 这段元叙述会被误当成答案抓回来，必须识别出来并继续等待。
AGENT_CHATTER: list[str] = [
    "打开相关页面", "并行搜索", "根据搜索结果", "现在需要根据",
    "接下来我将打开", "让我先搜索", "优先打开", "正在为您搜索",
    "获取更详细", "我需要先", "搜索结果来看", "已完成。现在",
    "正在浏览", "打开网页", "进入官网",
]


def is_agent_chatter(text: str) -> bool:
    """是否为 Agent 模式的中间进展文本，而不是真正的答案。"""
    if not text:
        return False
    t = text.strip()
    # 真正的答案通常较长；元叙述短且含特征短语
    if len(t) > 400:
        return False
    return any(p in t for p in AGENT_CHATTER)


@dataclass
class AskResult:
    ok: bool
    provider: str
    answer: str = ""
    error: str = ""
    elapsed: float = 0.0
    via: str = ""          # 用了哪一级策略抓到答案，便于排障
    shot: str = ""         # 出错时的截图文件名
    mode: dict = field(default_factory=dict)   # 各模式开关的最终状态
    images: list = field(default_factory=list)  # 抓到的生成图（本地路径）
    # ---- 仅纯 HTTP 直连路径会填 ----
    session_id: str = ""   # 远端会话 id（浏览器路径为空）
    resumed: bool = False  # 这轮是接上了上文，还是新开会话+回填历史


class BaseAdapter:
    """一个网页端聊天站点的驱动器。子类只需覆盖类属性。"""

    # ---- 元信息（子类必填） ----
    id: str = "base"
    name: str = "Base"
    url: str = ""
    homepage: str = ""          # 用于引导登录，缺省同 url
    # 该站点擅长的任务标签，用于路由打分
    tags: list[str] = []
    badge: str = ""             # 卡片上显示的小字，如 "每天免费额度"
    note: str = ""              # 控制台备注

    # ---- 选择器覆盖 ----
    selectors: Selectors = Selectors()
    # 命中这些祖先元素的候选**不算答案**。用于"思考过程与正文共用同一套
    # markdown 类名、只能靠所在面板区分"的站点（元宝）。
    # ★ 必须用这种"祖先路径黑名单 + JS closest()"的写法，
    #   不要写 CSS 的 `:not(.foo *)` —— 那是 CSS4 的复杂否定，
    #   本机 Chromium 实测**静默失效**（该选择器命中 0 个元素，
    #   不抛错、不告警，直接退化成"这个选择器不存在"，然后落到泛选择器上，
    #   把思考面板的内容当答案抓回来）。次选 `:not(.foo)` 只排除元素自身，
    #   管不了它的子孙，同样挡不住思考过程。
    answer_exclude: list[str] = []
    # 内容级剥离：**选中的答案容器内部**，命中这些选择器的子树要摘掉。
    #
    # 和 answer_exclude 的区别（别搞混，两者作用层不同）：
    #   · answer_exclude —— 候选元素级：命中这些祖先的**整个元素**不当候选；
    #   · answer_strip   —— 内容级：候选已经选定，但它内部混进了站点塞的
    #                       附加块（推荐视频卡片、猜你想问、广告位……），
    #                       这些块和正文在**同一个容器**里，换容器躲不开，
    #                       只能从克隆体里摘掉。
    # 元宝的推荐视频就是这样：结构 .hyc-common-markdown > .ybc-p >
    # .ybc-chat-videoBoxV2-bigCard-wrapper，和正文同属一个 markdown 容器，
    # 实测答案尾巴被挂上"相关视频00:44……艾袒心ADHD成长营2周前"。
    answer_strip: list[str] = []
    # 最近一次 snapshot_answer 选中了谁（排障用，见 /api/debug/dom?kind=snapshot）
    answer_pick: dict = {}
    # "有候选但被 answer_exclude 全排掉"这件事从什么时候开始的（0 = 正常）
    _excluded_since: float = 0.0
    # "答案抽取脚本连续抛错"从什么时候开始的（0 = 正常）
    _eval_error_since: float = 0.0

    # ---- 交互差异 ----
    enter_to_send: bool = True
    login_required: bool = True
    # 某些站点是 SPA，首次加载慢
    warmup_wait: float = 2.0
    # 发送前拼在用户提示词前面的引导语（比如让 Kimi 别把问题当 Agent 任务跑）
    prompt_prefix: str = ""
    # 模式开关：{按钮上的可见文字: 期望是否开启}。
    # 用文字定位而不是 class —— 前端构建后的 class 多为哈希值，每次发版都会变。
    # 状态靠 aria-pressed 判断，因此是幂等的，不会把已开启的又点关。
    mode_toggles: dict[str, bool] = {}
    # 另一类站点（元宝、豆包、通义、ChatGPT）没有开关，而是"选模型=选思考强度"：
    # 得先把下拉点开，再选菜单项。格式：{下拉触发元素的定位文字: 要选的菜单项文字}
    mode_pick: dict[str, str] = {}
    # 开启了深度思考这类慢模式时，把超时下限抬上去
    slow_mode_timeout: float = 240.0
    # 覆盖全局的"文本稳定多久算答完"。慢速流式的站点要调大 ——
    # 实测豆包 2.1 Turbo 输出中会有 >3.5s 的空档，用默认值会抓到半截答案。
    stable_window: float | None = None
    # 覆盖全局 ask_timeout。slow_mode_timeout 只会把超时"往上抬"，
    # 想给某家设一个更短的上限（避免按钮残留时空等）得用它。
    ask_timeout: float | None = None

    # ---- 纯 HTTP 直连（可选能力）----
    # 置 True 的站点可以不开浏览器直接取答案，更快也更省资源。
    # 子类实现 ask_http()；返回 None 表示"这条路走不通"，调用方退回浏览器。
    http_capable: bool = False

    async def ask_http(self, prompt: str, *, thread: str = "",
                       reset: bool = False, timeout: float | None = None,
                       no_mode: bool = False, grab_images: bool = False,
                       should_stop=None) -> "AskResult | None":
        """不开浏览器问一次。默认不支持，子类按需覆盖。"""
        return None

    async def harvest_credentials(self, page) -> None:
        """走完浏览器路线后，顺手把凭证收下来给 HTTP 路径用。"""
        return None

    def __init__(self):
        self._conf_cache: dict | None = None
        self.mode_report: dict[str, Any] = {}
        # 本轮清掉了哪些浮层，形如 ["round1:Esc", "round1:site:xxx"]
        self.popup_report: list[str] = []

    # ------------------------------------------------------------ 配置合并
    @property
    def conf(self) -> dict:
        from . import settings
        if self._conf_cache is None:
            self._conf_cache = settings.provider_conf(self.id)
        return self._conf_cache

    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def _merge(self, key: str, generic: list[str]) -> list[str]:
        """厂商选择器在前，通用兜底在后，再去重。"""
        custom = list(getattr(self.selectors, key) or [])
        from . import settings
        user = list(self.conf.get("selectors", {}).get(key, []) or [])
        out: list[str] = []
        for s in custom + user + generic:
            if s and s not in out:
                out.append(s)
        return out

    # ------------------------------------------------------------ 弹窗清理
    async def _has_popup(self, page: Page) -> bool:
        for sel in POPUP_ROOTS:
            if await self._visible(page, sel):
                try:
                    box = await page.locator(sel).first.bounding_box()
                    # 太小的多半是站点自带的同名小部件（比如某个叫 xxx-overlay 的
                    # 渐变蒙层），不是盖住我们的浮层
                    if box and box["width"] * box["height"] > 40000:
                        return True
                except Exception:
                    continue
        return False

    async def dismiss_popups(self, page: Page, *, rounds: int = 2) -> list[str]:
        """清掉挡路的浮层，返回每个被关掉的东西的描述（便于排障）。

        由无害到激进分四级，**任一级成功就停**：

            1. Esc 键      —— 多数 overlay 都监听它，且不会破坏页面状态
            2. 站点选择器  —— providers.<id>.selectors.popup_close 里配的
            3. 通用启发式  —— 弹窗容器里找"文案像关闭"的可点元素
            4. 点遮罩空白  —— 有些浮层点外面就退

        全程限时约 3 秒，失败一律静默：**关不掉弹窗不该让整次提问挂掉**，
        顶多后续点击超时报错，那也是原本会发生的事。
        """
        closed: list[str] = []
        try:
            if not await self._has_popup(page):
                return closed
        except Exception:
            return closed

        for r in range(max(1, rounds)):
            did = False

            # ---- 1) Esc：先试最省事的。注意别在生成答案期间调它。
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(250)
                if not await self._has_popup(page):
                    closed.append(f"round{r+1}:Esc")
                    return closed
            except Exception:
                pass

            # ---- 2) 站点自定义关闭按钮
            for sel in self._merge("popup_close", []):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible(timeout=500):
                        await loc.click(timeout=2000)
                        await page.wait_for_timeout(300)
                        if not await self._has_popup(page):
                            closed.append(f"round{r+1}:site:{sel}")
                            return closed
                except Exception:
                    continue

            # ---- 3) 通用启发式：文案/aria-label 命中"关闭"语义的小按钮
            n = await self._tag_close_candidates(page)
            for i in range(n):
                try:
                    loc = page.locator(f"[data-ts-popup-close='{i}']").first
                    if not await loc.count():
                        continue
                    await loc.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    if not await self._has_popup(page):
                        closed.append(f"round{r+1}:heuristic#{i}")
                        return closed
                    did = True
                except Exception:
                    continue
            try:
                await page.evaluate(
                    "() => document.querySelectorAll('[data-ts-popup-close]')"
                    ".forEach(e => e.removeAttribute('data-ts-popup-close'))")
            except Exception:
                pass

            # ---- 4) 点遮罩空白处（右上角，避开内容）
            if not did:
                try:
                    vp = page.viewport_size or {"width": 1280, "height": 800}
                    await page.mouse.click(vp["width"] - 8, 8)
                    await page.wait_for_timeout(300)
                    if not await self._has_popup(page):
                        closed.append(f"round{r+1}:backdrop")
                        return closed
                except Exception:
                    pass

            # 这一轮没关掉，先喘口气再试下一轮
            await page.wait_for_timeout(400)

        return closed

    async def _tag_close_candidates(self, page: Page) -> int:
        """给"像是关闭按钮"的元素打临时标记，返回候选数量。

        判定必须从严：只认**小面积**且**文案/aria-label/title 命中关闭词**的
        可点元素。宁可漏掉（下一轮 Esc/遮罩兜底），也不能把页面上的正常按钮
        当成关闭键点掉 —— 那条路会做出用户看不懂的诡异行为。
        """
        try:
            return await page.evaluate(
                """([roots, words]) => {
                  const norm = (s) => (s || '').trim().toLowerCase();
                  const hitWord = (txt) => {
                    const t = norm(txt);
                    if (!t) return false;
                    // 短文本才允许匹配：长段落里含"取消"也当没看见
                    if (t.length > 20) return false;
                    return words.some(w => t.includes(norm(w)));
                  };
                  const clickable = 'button, [role="button"], a, [class*="close" i], [class*="cancel" i]';
                  const out = [];
                  let n = 0;

                  // A) 浮层容器里的候选
                  const containers = [];
                  for (const rs of roots) {
                    document.querySelectorAll(rs).forEach(e => containers.push(e));
                  }
                  for (const c of containers) {
                    const cr = c.getBoundingClientRect();
                    if (cr.width * cr.height < 40000) continue;   // 小部件不算浮层
                    c.querySelectorAll(clickable).forEach(e => {
                      const r = e.getBoundingClientRect();
                      if (!r.width || !r.height) return;
                      if (r.width * r.height > 20000) return;      // 关闭键不会很大
                      const txt = e.innerText || '';
                      const meta = (e.getAttribute('aria-label') || '') + ' '
                                 + (e.getAttribute('title') || '');
                      if (!hitWord(txt) && !hitWord(meta)) return;
                      e.setAttribute('data-ts-popup-close', String(n++));
                      out.push(e);
                    });
                  }

                  // B) 有些站点浮层不带任何 modal class，补一轮全局扫描
                  if (!n) {
                    document.querySelectorAll(clickable).forEach(e => {
                      const r = e.getBoundingClientRect();
                      if (!r.width || !r.height) return;
                      if (r.width * r.height > 20000) return;
                      const meta = (e.getAttribute('aria-label') || '') + ' '
                                 + (e.getAttribute('title') || '');
                      if (!hitWord(meta)) return;                  // 只认显式标注，不放宽到正文
                      e.setAttribute('data-ts-popup-close', String(n++));
                      out.push(e);
                    });
                  }
                  return n;
                }""", [POPUP_ROOTS, POPUP_CLOSE_WORDS])
        except Exception:
            return 0

    # ------------------------------------------------------------ 元素定位
    async def _visible(self, page: Page, sel: str) -> bool:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                return False
            return await loc.is_visible(timeout=800)
        except Exception:
            return False

    async def find_input(self, page: Page):
        """逐级找输入框；同级有多个时取面积最大的可见者。"""
        for sel in self._merge("input", GENERIC_INPUT_SELECTORS):
            try:
                locs = page.locator(sel)
                n = await locs.count()
                if n == 0:
                    continue
                if n == 1:
                    if await locs.first.is_visible():
                        return locs.first, f"input:{sel}"
                    continue
                # 多个候选 → 取最大可见
                best, best_area = None, -1.0
                for i in range(min(n, 8)):
                    cand = locs.nth(i)
                    if not await cand.is_visible():
                        continue
                    box = await cand.bounding_box()
                    if not box:
                        continue
                    area = max(0.0, box["width"]) * max(0.0, box["height"])
                    if area > best_area:
                        best, best_area = cand, area
                if best is not None:
                    return best, f"input:{sel}"
            except Exception:
                continue
        return None, ""

    # ------------------------------------------------------------ 登录态
    async def is_logged_in(self, page: Page) -> bool:
        for sel in self._merge("input", LOGGED_IN_MARKERS):
            if await self._visible(page, sel):
                return True
        return False

    async def wait_for_login(self, page: Page, timeout: float = 300.0) -> bool:
        """登录窗口专用：轮询直到输入框出现。

        登录成功会顺手把凭证存盘 —— 这一步不能省：多数站点的会话凭证是
        session cookie，浏览器一关就被清，不存盘下次还得重新扫码。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await self.is_logged_in(page):
                try:
                    from . import browser
                    await browser.manager.save_session(self.id)
                except Exception:
                    pass
                return True
            await page.wait_for_timeout(1000)
        return False

    # ------------------------------------------------------------ 新会话
    async def new_chat(self, page: Page) -> bool:
        """开一个新会话，避免上一轮上下文污染答案并拖慢速度。"""
        for sel in self._merge("new_chat", NEW_CHAT_HINTS):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(1200)
                    return True
            except Exception:
                continue
        # 退化：重新导航到首页（多数站点会落在空白新会话）
        try:
            await page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ 发送
    @property
    def toggles(self) -> dict[str, bool]:
        """类里的默认值 + config.yaml 里 providers.<id>.toggles 的覆盖。"""
        out = dict(self.mode_toggles)
        for k, v in (self.conf.get("toggles") or {}).items():
            out[k] = bool(v)
        return out

    # 每次调用的结果，供上层回显：{开关名: "on"|"off"|"already"|"notfound"|"fail"}
    mode_report: dict[str, str] = {}

    _TOGGLE_STATE_JS = """(el) => {
        const attrs = %s;
        for (const a of attrs) {
            const v = el.getAttribute(a);
            if (v !== null && v !== undefined && v !== '') return String(v).toLowerCase();
        }
        const cls = (el.className && el.className.toString) ? el.className.toString() : '';
        const hit = new RegExp(%s, 'i').exec(cls);
        if (hit) return hit[2].toLowerCase();
        return '';
    }""" % (
        "[" + ",".join("'%s'" % a for a in TOGGLE_STATE_ATTRS) + "]",
        "'(^|[\\\\s_-])(active|selected|on|checked|enabled)([\\\\s_-]|$)'",
    )

    async def _find_toggle(self, page: Page, label: str):
        """定位开关元素。

        按语义强度降序尝试；命中多个时取**可见且面积最小**的那个 ——
        `has-text` 会一路命中外层容器，取最小才能拿到真正的按钮本体。
        """
        # label 里带 [ = ，或以 . / # 开头的，视为原生 CSS 选择器直接使用。
        # 站点若给开关加了语义 class / data-* 测试属性，这条路最稳
        # （不受文案改版影响），如元宝的
        # [data-thinking-mode-switcher-trigger='true']、Kimi 的
        # .kimi-menu-trigger-wrapper。
        if "[" in label or "=" in label or label.startswith((".", "#")):
            cands = [label]
        else:
            cands = [
                f"[aria-pressed]:has-text('{label}')",
                f"[aria-checked]:has-text('{label}')",
                f"[role='switch']:has-text('{label}')",
                # 有些按钮只有图标/aria-label
                f"[aria-label*='{label}']",
                f"[role='button']:has-text('{label}')",
                f"button:has-text('{label}')",
                f"label:has-text('{label}')",
                f"div:has-text('{label}')",
            ]
        for sel in cands:
            try:
                locs = page.locator(sel)
                n = await locs.count()
                if n == 0:
                    continue
                best, best_area = None, None
                for i in range(min(n, 15)):
                    l = locs.nth(i)
                    try:
                        if not await l.is_visible(timeout=400):
                            continue
                        bb = await l.bounding_box()
                        if not bb or bb["width"] <= 0 or bb["height"] <= 0:
                            continue
                        area = bb["width"] * bb["height"]
                        if area > 160_000:      # 太大，多半是整块工具栏
                            continue
                        if best_area is None or area < best_area:
                            best, best_area = l, area
                    except Exception:
                        continue
                if best is not None:
                    return best
            except Exception:
                continue
        return None

    async def apply_mode(self, page: Page) -> bool:
        """打开/关闭站点上的模式开关（深度思考、联网搜索之类）。

        返回是否处于"慢模式"（有任何一个开关最终落在开启态）。
        失败不抛错 —— 开关点不动不该阻断提问本身。
        每个开关的最终状态写进 self.mode_report，供上层回显给用户。
        """
        slow = False
        self.mode_report = {}
        for label, want in self.toggles.items():
            try:
                loc = await self._find_toggle(page, label)
                if loc is None:
                    self.mode_report[label] = "notfound"
                    continue

                def _on(raw: str) -> bool:
                    return (raw or "").strip().lower() in TOGGLE_TRUE_VALUES

                raw = (await loc.evaluate(self._TOGGLE_STATE_JS)) or ""
                cur = _on(raw)
                if not raw.strip():
                    # 读不到任何状态标记。这时点下去有 50% 概率把本来就开着的
                    # 思考模式给关掉，宁可不动 —— 报 unknown 让人去确认。
                    self.mode_report[label] = "unknown"
                    continue
                if cur == want:
                    self.mode_report[label] = "on" if cur else "off"
                    if want:
                        slow = True
                    continue

                await loc.click(timeout=4000)
                await page.wait_for_timeout(500)
                new = _on(await loc.evaluate(self._TOGGLE_STATE_JS))
                if new != want:      # 没切成功，再点一次
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    new = _on(await loc.evaluate(self._TOGGLE_STATE_JS))

                self.mode_report[label] = ("on" if new else "off") if new == want else "fail"
                if want and new:
                    slow = True
            except Exception:
                self.mode_report.setdefault(label, "fail")
                continue
        return slow

    async def _find_menu_item(self, page: Page, item: str):
        """在已展开的菜单里找某一项。

        优先级：带 aria-checked/aria-selected 的元素最可信 —— 那说明它是个
        真正的可选项（元宝的菜单项就是 <button aria-checked>，没有 role）。
        其次才是 role=menuitem/option，最后退到按文字取最小可见元素。
        """
        cands = [
            f"[aria-checked]:has-text('{item}')",
            f"[aria-selected]:has-text('{item}')",
            # 注意 role 有 menuitem / menuitemradio / menuitemcheckbox 三种，
            # Kimi 用的是 menuitemradio —— 只写 menuitem 会漏掉
            f"[role='menuitem']:has-text('{item}')",
            f"[role='menuitemradio']:has-text('{item}')",
            f"[role='menuitemcheckbox']:has-text('{item}')",
            f"[role='option']:has-text('{item}')",
            f"li:has-text('{item}')",
            f"[role='listitem']:has-text('{item}')",
            f"button:has-text('{item}')",
            f"div:has-text('{item}')",
        ]
        for sel in cands:
            try:
                locs = page.locator(sel)
                n = await locs.count()
                if n == 0:
                    continue
                best, best_area = None, None
                for i in range(min(n, 20)):
                    l = locs.nth(i)
                    try:
                        if not await l.is_visible(timeout=400):
                            continue
                        bb = await l.bounding_box()
                        if not bb or bb["width"] <= 0:
                            continue
                        area = bb["width"] * bb["height"]
                        if area > 200_000:
                            continue
                        if best_area is None or area < best_area:
                            best, best_area = l, area
                    except Exception:
                        continue
                if best is not None:
                    return best
            except Exception:
                continue
        return None

    async def apply_pick(self, page: Page) -> bool:
        """展开下拉并选中指定菜单项（元宝的"深度思考"、ChatGPT 的推理模型等）。

        返回是否切进了"慢模式"。同样不抛错 —— 选不到就当普通模式继续问。
        """
        slow = False
        for trigger, item in (self.picks or {}).items():
            key = f"pick:{item}"
            try:
                # 触发按钮上的文字会随当前选中项变化（元宝在"快速回答/深度思考/
                # 专家模式"之间切），所以支持用 | 分隔多个候选，依次试。
                #
                # 找 3 轮再放弃：浏览器被关掉重建后，页面要重新加载，
                # 模型选择器往往还没渲染出来。只找一次就判定 notfound 的话，
                # 就会出现"浏览器重开之后模型退回默认、深度思考/高速模式没打开"。
                loc = None
                for attempt in range(3):
                    for lb in [x.strip() for x in trigger.split("|") if x.strip()]:
                        loc = await self._find_toggle(page, lb)
                        if loc is not None:
                            break
                    if loc is not None:
                        break
                    await page.wait_for_timeout(1500)      # 等页面把控件渲染出来
                if loc is None:
                    self.mode_report[key] = "notfound"
                    continue
                # 幂等：触发按钮上已经写着目标项（说明当前就是它），别再点开
                cur = ((await loc.text_content()) or "").strip()
                if item in cur:
                    self.mode_report[key] = "on"
                    slow = True
                    continue

                await loc.click(timeout=4000)
                await page.wait_for_timeout(1200)     # 实测元宝菜单渲染要 >1s
                mi = await self._find_menu_item(page, item)
                if mi is None:
                    # 有可能是"菜单本来就开着，这一点反而把它关上了"，再点一次
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(1200)
                    mi = await self._find_menu_item(page, item)
                if mi is None:
                    self.mode_report[key] = "notfound"
                    # 菜单点开了却没找到项 —— 按 Esc 收掉，别挡着输入框
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    continue
                await mi.click(timeout=4000)
                await page.wait_for_timeout(600)
                # 复核：触发按钮文字里应出现目标项
                try:
                    after = ((await loc.text_content()) or "").strip()
                except Exception:
                    after = ""
                ok = item in after
                self.mode_report[key] = "on" if ok else "fail"
                if ok:
                    slow = True
            except Exception:
                self.mode_report.setdefault(key, "fail")
                continue
        return slow

    @property
    def picks(self) -> dict[str, str]:
        """类默认 + config.yaml 里 providers.<id>.picks 的覆盖。"""
        out = dict(self.mode_pick)
        for k, v in (self.conf.get("picks") or {}).items():
            out[k] = str(v)
        return out

    async def _read_input(self, loc) -> str:
        """读回输入框当前内容，用来校验文字到底有没有真的输进去。"""
        try:
            got = await loc.evaluate(
                """el => (el.value !== undefined && el.value !== null)
                          ? el.value
                          : (el.innerText || el.textContent || '')""")
            return got or ""
        except Exception:
            return ""

    async def _type(self, page: Page, loc, text: str) -> None:
        """把文字送进输入框。三级兜底 + **每次校验**。

        这里踩过一个坑：原来长文本走"写系统剪贴板 + Ctrl+V"，
        而 navigator.clipboard.writeText 在非焦点窗口里会静默失败，
        于是粘贴进去的是剪贴板里的旧内容、甚至只有一个字符 ——
        实测 Kimi 只收到了一个"（"。所以现在改成：
            1) 合成 paste 事件（不碰系统剪贴板，且保留换行语义）
            2) Playwright fill（原生支持 contenteditable）
            3) 逐字 type（最慢但最稳）
        每级之后都读回内容比对，长度对得上才认为成功。
        """
        # 空文本直接返回、什么都别做。
        # 这里踩过坑：调用方常"先单独输入正文、再用 send(page, '') 只点发送"，
        # 若此处对空串执行 fill('')，会把刚输好的内容清空 —— 消息就发不出去了。
        if not text:
            return
        await loc.click()
        need = text.strip()

        if len(text) > 200:
            try:
                await loc.evaluate(
                    """(el, v) => {
                        el.focus();
                        const dt = new DataTransfer();
                        dt.setData('text/plain', v);
                        el.dispatchEvent(new ClipboardEvent('paste', {
                            clipboardData: dt, bubbles: true, cancelable: true
                        }));
                    }""", text)
                await page.wait_for_timeout(350)
            except Exception:
                pass
            if len((await self._read_input(loc)).strip()) >= len(need) * 0.8:
                return

        try:
            await loc.fill(text)
            await page.wait_for_timeout(150)
            if len((await self._read_input(loc)).strip()) >= len(need) * 0.8:
                return
        except Exception:
            pass

        try:
            await loc.fill("")
        except Exception:
            pass
        await loc.type(text, delay=1)
        await page.wait_for_timeout(150)

    async def send(self, page: Page, text: str) -> str:
        inp, via = await self.find_input(page)
        if inp is None:
            raise RuntimeError("找不到输入框，页面结构可能已变化或未登录")
        await self._type(page, inp, text)
        # 发出前把内容读回来核一遍。宁可报错重来，也别把半截问题发出去 ——
        # 实测过一次只发出去一个"（"，对面一脸懵地反问"你是误发吗"。
        got = (await self._read_input(inp)).strip()
        want = text.strip()
        if want and len(got) < len(want) * 0.5:
            raise RuntimeError(
                f"输入未生效（框内 {len(got)} 字 / 应为 {len(want)} 字），"
                f"已中止发送以免发出残缺内容：{got[:40]!r}")
        await page.wait_for_timeout(300)

        # 发送，并**确认输入框真的清空了** —— 那才是"消息已发出"的可靠标志。
        # 实测豆包出现过"按钮点下去了但消息没发出去、文字还留在框里"的情况，
        # 用户看到的就是消息卡在输入框里（还得自己手动点一下发送）。
        # 所以这里逐级尝试并逐级校验，最后仍发不出去就明确报错。
        sent = False
        for sel in self._merge("send_button", SEND_BUTTON_HINTS):
            try:
                loc = page.locator(sel).first
                if not (await loc.count() and await loc.is_visible()):
                    continue
                # 必须是真正的可点元素。实测豆包的 [data-testid*='send']
                # 命中的是**用户消息气泡** <div data-testid="send_message">，
                # 点它等于没点 —— 消息就卡在输入框里，用户得自己手动发。
                is_btn = await loc.evaluate(
                    "el => { const t = (el.tagName || '').toLowerCase();"
                    "  return t === 'button' || el.getAttribute('role') === 'button'; }")
                if not is_btn:
                    continue
                await loc.click(timeout=5000)
                await page.wait_for_timeout(900)
                if not (await self._read_input(inp)).strip():
                    via += " +button"
                    sent = True
                    break
            except Exception:
                continue

        if not sent and self.enter_to_send:
            try:
                await inp.press("Enter")
                await page.wait_for_timeout(900)
                if not (await self._read_input(inp)).strip():
                    via += " +enter"
                    sent = True
            except Exception:
                pass

        if not sent and (text or "").strip():
            raise RuntimeError(
                "消息没能发出去：点了发送按钮但输入框内容仍在。"
                "请在浏览器里手动发一次，或到 config.yaml 里给该站点补 send_button 选择器。")
        return via

    # ------------------------------------------------------------ 等待完成
    async def _generating(self, page: Page) -> bool:
        for sel in self._merge("stop_button", STOP_HINTS):
            if await self._visible(page, sel):
                return True
        for sel in STREAMING_HINTS:
            if await self._visible(page, sel):
                return True
        return False

    async def _thinking_text(self, page: Page) -> bool:
        """页面里出现'思考中/正在生成'等字样（排除掉答案正文里的引用很难，
        所以只在答案容器之外找，且要求是短文案节点）。"""
        try:
            hits = await page.evaluate(
                """(words) => {
                    const out = [];
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT);
                    let n, count = 0;
                    while ((n = walker.nextNode()) && count < 400) {
                        const t = (n.textContent || '').trim().toLowerCase();
                        if (!t || t.length > 30) continue;
                        const el = n.parentElement;
                        if (!el) continue;
                        const _b = el.getBoundingClientRect();
                        if (!_b.width || !_b.height) continue;   // 不可见
                        const cls = (el.className || '').toString();
                        if (/markdown|message|content|answer/i.test(cls)) continue;
                        for (const w of words) {
                            if (t.includes(w)) { out.push(t); break; }
                        }
                        count++;
                    }
                    return out.length > 0;
                }""",
                [w.lower() for w in THINKING_TEXTS],
            )
            return bool(hits)
        except Exception:
            return False

    async def _busy(self, page: Page) -> bool:
        return await self._generating(page) or await self._thinking_text(page)

    async def _stop_visible(self, page: Page) -> bool:
        """只看'停止'按钮 —— 这是唯一完成态与进行态都可靠的信号；
        thinking/streaming 类元素生成结束后常常残留在页面上，不能用于终态判定。"""
        for sel in self._merge("stop_button", STOP_HINTS):
            if await self._visible(page, sel):
                return True
        return False

    async def wait_done(self, page: Page, timeout: float = 180.0,
                        stable_window: float = 3.0, *,
                        baseline: str = "") -> None:
        """三阶段等待：
        1) 等生成开始的信号（停止按钮 / 思考中 / 答案开始变化）；
        2) 若见到过'停止'按钮，等它消失（说明流式输出真正结束）；
        3) 文本连续 stable_window 秒不变才算完。
        解决'思考中文本不动 → 误判已完成'和'完成态残留 → 永远等不完'两类问题。"""
        deadline = time.time() + timeout
        t0 = time.time()
        # 停机要能立刻生效：深度思考一次要等几十秒到三分钟，
        # 若只在提问前后检查，用户触发停机后还得干等它跑完。这里每个轮询周期
        # 都查一次，秒级中断。
        from .killswitch import switch

        # ---- 阶段 1：等开始（最多 45s；等不到也继续，交给阶段 3 兜底）
        start_deadline = time.time() + 45
        while time.time() < start_deadline and time.time() < deadline:
            switch.check()
            # 页面被关掉了就别等了 —— 否则会一路傻等到 timeout（实测白等过 300s，
            # 用户那边看到的是"网页明明答完了，程序却卡着不动"）
            if page.is_closed():
                raise TimeoutError("页面已被关闭，无法继续等待回答")
            if await self._busy(page):
                break
            cur = await self.snapshot_answer(page)
            # Agent 模式的"检索计划"不算答案，继续等
            if cur and cur != baseline and not is_agent_chatter(cur):
                break
            self._check_exclude_sanity()
            await page.wait_for_timeout(400)

        # ---- 阶段 2：等停止按钮消失（带上限）
        # 有些站点（实测豆包）生成结束后按钮仍残留在 DOM 里，
        # 若无限等就会一路耗到 deadline —— 实测白等 240s。
        # 所以这里设个上限，剩下的交给阶段 3 判文本稳定。
        stop_deadline = time.time() + min(45.0, max(12.0, timeout * 0.3))
        while time.time() < stop_deadline and await self._stop_visible(page):
            switch.check()
            await page.wait_for_timeout(500)

        # ---- 阶段 3：文本稳定
        # 判据是"连续 stable_window 秒文本不变"，但**只要停止按钮还在，
        # 就说明确实还在生成**，此时把稳定计时清零。
        # 这一步不能省：慢速站点（如豆包联网搜索）输出中间会有 >5s 的空档，
        # 只看文本会把"2026 年 9 月"这种半截内容当成答案收工。
        # 残留按钮的风险由 deadline 兜住（超时后仍会抓取现有内容）。
        last, stable_since = "", time.time()
        while time.time() < deadline:
            switch.check()
            if page.is_closed():        # 同上：页面没了立刻失败，不空等
                raise TimeoutError("页面已被关闭，无法继续等待回答")
            cur = await self.snapshot_answer(page)
            now = time.time()
            if cur != last:
                last, stable_since = cur, now
            elif is_agent_chatter(cur):
                stable_since = now          # 还在检索/翻页，不算稳定
            elif (now - t0) < 90 and await self._stop_visible(page):
                # 停止按钮还在 → 判断为仍在生成。但这个判据只在**前 90 秒**认，
                # 否则个别站点残留的停止按钮会让稳定计时永远被重置，一路空等
                # 到 timeout（实测 DeepSeek 白等过 300 秒，用户以为程序卡死了）。
                stable_since = now
            elif cur and cur != baseline \
                    and (now - stable_since) >= stable_window \
                    and (now - t0) >= 6.0:
                return
            self._check_exclude_sanity()
            await page.wait_for_timeout(600)
        raise TimeoutError(f"{self.name} 回答超时（{timeout}s）")

    # ------------------------------------------------------------ 抓取答案
    async def _click_last_copy(self, page: Page) -> str:
        """点最后一个"复制"按钮并从剪贴板读取 —— 最稳的一条路。"""
        try:
            ctx = page.context
            await ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                                        origin=page.url)
        except Exception:
            return ""
        for sel in self._merge("copy_button", COPY_BUTTON_HINTS):
            try:
                locs = page.locator(sel)
                n = await locs.count()
                if n == 0:
                    continue
                await locs.nth(n - 1).click(timeout=3000)
                await page.wait_for_timeout(400)
                text = await page.evaluate("() => navigator.clipboard.readText()")
                if text and text.strip():
                    return text.strip()
            except Exception:
                continue
        return ""

    async def snapshot_answer(self, page: Page) -> str:
        """返回当前会话里"最后一条"答案文本（可能是流式中间态）。
        内含表格时先转成 Markdown，避免结构丢失。

        选择逻辑全在 PICK_ANSWER_JS 里（一次 evaluate 搞定，省掉
        "逐个候选往返问可见性"的开销 —— 流式等待期间这个函数每秒都被调，
        往返次数直接决定轮询间隔能不能压到 600ms）。"""
        excluded_any = 0
        eval_error = ""
        for sel in self._merge("answer", GENERIC_ANSWER_SELECTORS):
            try:
                picked = await page.evaluate(
                    PICK_ANSWER_JS, [sel, list(self.answer_exclude),
                                     list(self.answer_strip), 60000])
            except Exception as e:  # noqa: BLE001
                # ★ 这里**绝不能**默默 continue。
                #   当初 TABLE_TO_MD_JS 有个 JS 语法错（注释里的 \n 被 Python
                #   解释成真换行），每次 evaluate 都抛 SyntaxError，
                #   却被一句 `except Exception: 退回 inner_text` 吞掉了 ——
                #   结果是"表格转 Markdown / 公式还原 / 清角标"整整三项功能
                #   从来没生效过，几个月都没人发现（见 _TO_MD_JS 上方注释）。
                #   现在把最后一次错误留在 answer_pick 里，`/api/debug/dom`
                #   的 snapshot / watch 都能看到；同时 _check_eval_sanity
                #   会在连续报错 30 秒后主动抛错，不再一路空等到超时。
                eval_error = f"{type(e).__name__}: {e}".split("\n")[0][:300]
                continue
            if picked and picked.get("text"):
                self.answer_pick = {
                    "selector": sel,
                    "index": picked.get("idx"),
                    "candidates": picked.get("total"),
                    "nested_dropped": picked.get("nested_dropped"),
                    "len": len(picked["text"]),
                }
                self._excluded_since = 0.0
                self._eval_error_since = 0.0
                return picked["text"]
            if picked:
                excluded_any = max(excluded_any, int(picked.get("excluded") or 0))
                self.answer_pick = {"selector": sel, "empty": True,
                                    "raw": picked.get("raw"),
                                    "excluded": picked.get("excluded")}
        if eval_error and not excluded_any:
            if not self._eval_error_since:
                self._eval_error_since = time.time()
            self.answer_pick = {"eval_error": eval_error}
        else:
            self._eval_error_since = 0.0
        # ---- 防呆：候选存在，但全被 answer_exclude 排掉了 ----
        # 这是**配置写错**的典型症状（比如把"整条消息外壳"当成"思考面板"排掉），
        # 但表现出来和"站点变慢"完全一样：抓不到答案 → 一路等到 ask_timeout
        # → 再退到别的站点从头重来。实测这样白烧 240 秒 + 一次兜底，
        # 事后看日志只能看到一句"超时"，根本猜不到是选择器的事。
        # 所以在这里单独记一笔，超过 30 秒就由 wait_done 主动报错。
        if excluded_any:
            if not self._excluded_since:
                self._excluded_since = time.time()
        else:
            self._excluded_since = 0.0
        return ""

    def _check_exclude_sanity(self) -> None:
        """抽取器连坐 30 秒 → 立刻报错，别等到超时。

        两种情况合并处理，因为外行看起来完全一样（都是"抓不到答案"），
        但根因都是**配置/代码写错了**，继续等下去没有任何意义：
          · answer_exclude 把候选全排掉了；
          · page.evaluate 每次都抛错（JS 语法错、选择器非法……）。
        不主动报的话，只会一路空等到 ask_timeout（实测 240 秒），
        再退到别的站点从头重来一遍，日志上只留一句"超时"。
        """
        now = time.time()
        if self._excluded_since and now - self._excluded_since > 30:
            raise RuntimeError(
                f"{self.name} 的 answer_exclude={self.answer_exclude} "
                f"把答案候选全部排除了（最近一次选择结果：{self.answer_pick}）。"
                "多半是把'整条消息的外壳容器'当成了'思考过程容器' —— "
                "外壳里同时装着思考和答案，排掉整壳等于把答案一起排掉。"
                "请用 /api/debug/dom?kind=answer_probe 确认该排除的是哪一层。")
        if self._eval_error_since and now - self._eval_error_since > 30:
            raise RuntimeError(
                f"{self.name} 的答案抽取脚本连续抛错 30 秒，最后一次是："
                f"{self.answer_pick.get('eval_error')}。"
                "这类错误以前被 `except Exception: 退回 inner_text` 吞掉过，"
                "导致功能静默失效（表格没转 Markdown、公式没还原、角标没清）。"
                "请先用 tools/selftest_answer_pick.py 校验 JS 语法。")

    async def extract_images(self, page: Page, *, min_side: int = 512,
                             exclude: "set[str] | None" = None,
                             limit: int = 6) -> list[str]:
        """捞出答案区里"像生成结果"的图片 URL（按面积降序，已去重）。

        过滤办法很朴素但有效：头像/图标/占位图的 naturalWidth 都很小，
        真正生成出来的图至少 512px 起。data: 占位（加载中）也排除掉。

        `exclude` 是**发问之前**页面上已有的那些图，用来把"这轮新生成的图"
        和"历史消息里还没滚走的旧图"分开 —— 见 `_ask_inner` 里的 img_baseline。
        没有这个参数的话，只要复用页面会话（reset=False），抓图就会把
        上一轮的图当成这一轮的结果重新下载一遍（实测豆包两轮不同提示词
        抓回来的 4 张图 **SHA256 完全相同**）。

        `limit` 默认 6（够用即可）；拍基线快照时调用方会传一个很大的值，
        因为"页面现在有哪些图"必须**收全**，漏一张就会把它当成新图。

        ★ 排除必须在**切片之前**做（所以在 JS 里 filter，而不是拿回来再滤）：
        历史会话里旧图一多，按面积排序后新图会被挤到 limit 之外，
        拿回来再滤就只剩空数组 —— 表现为"这轮没生成图"，其实是看漏了。
        """
        try:
            urls = await page.evaluate(
                """([minSide, limit, exclude]) => {
                    const drop = new Set(exclude || []);
                    const out = [];
                    for (const e of document.querySelectorAll('img')) {
                      const _r = e.getBoundingClientRect();
                      if (!_r.width || !_r.height) continue;
                      const w = e.naturalWidth || 0, h = e.naturalHeight || 0;
                      if (w < minSide || h < minSide) continue;
                      const src = e.currentSrc || e.src || '';
                      if (!src || src.startsWith('data:')) continue;
                      if (drop.has(src)) continue;
                      out.push({src: src, area: w * h});
                    }
                    out.sort((a, b) => b.area - a.area);
                    const seen = new Set(), uniq = [];
                    for (const i of out) {
                      if (seen.has(i.src)) continue;
                      seen.add(i.src); uniq.push(i.src);
                    }
                    return uniq.slice(0, limit);
                }""", [min_side, limit, sorted(exclude) if exclude else []])
        except Exception:
            return []
        return urls

    async def download_images(self, page: Page, urls: list[str],
                              *, referer: str = "") -> list[str]:
        """把图片下载到 data/images/，返回本地绝对路径。

        走 page.request 而不是页面内 fetch：后者受同源策略约束，
        会被 CDN 的 CORS 挡下（实测豆包的图床就是这种情况）。
        page.request 用独立的网络栈，并自动带上 context 的 cookie。
        """
        from . import settings
        files: list[str] = []
        hdrs = {"Referer": referer} if referer else {}
        for u in urls:
            try:
                resp = await page.request.get(u, headers=hdrs)
                if not resp.ok:
                    continue
                blob = await resp.body()
                if len(blob) < 4096:      # 太小的多半是占位/错误页
                    continue
                ct = (resp.headers.get("content-type") or "").lower()
                if "jpeg" in ct or "jpg" in ct:
                    ext = "jpg"
                elif "webp" in ct:
                    ext = "webp"
                elif "gif" in ct:
                    ext = "gif"
                else:
                    ext = "png"
                p = settings.IMAGE_DIR / f"{self.id}-{int(time.time() * 1000)}.{ext}"
                p.write_bytes(blob)
                files.append(str(p))
            except Exception:
                continue
        return files

    async def extract(self, page: Page) -> tuple[str, str]:
        """拿到最终答案。返回 (文本, 策略)。

        优先走"复制"按钮拿 Markdown 原文 —— 表格、代码块、公式的结构
        只有在原文里才完整；渲染后的 innerText 会把表格拉散。
        拿不到再退回 DOM 抓取（DOM 那一路已把 <table> 转成 Markdown）。
        """
        dom = ""
        try:
            dom = await self.snapshot_answer(page)
        except Exception as e:  # noqa: BLE001
            # 不静默吞：DOM 路抓不到时，下面会退回剪贴板，最终可能仍返回一份
            # "看起来正常"的答案 —— 但结构（表格/公式）已经丢了。
            # 留痕，方便事后从 via 字段看出走的是哪条路。
            self.answer_pick = {"extract_error": f"{type(e).__name__}: {e}"[:200]}
            dom = ""

        clip = ""
        try:
            clip = await self._click_last_copy(page)
        except Exception:  # noqa: BLE001
            clip = ""

        if clip and len(clip.strip()) >= 20:
            # 站点给的原文通常比渲染文本更长（含 Markdown 标记），信息量更大
            if not dom or len(clip) >= len(dom) * 0.85:
                return clip.strip(), "clipboard(Markdown原文)"
        if dom:
            return dom, "dom"
        if clip:
            return clip.strip(), "clipboard"
        return "", "none"

    # ------------------------------------------------------------ 主流程
    async def ask(self, page: Page, prompt: str, *, reset: bool = True,
                  timeout: float = 180.0, stable_window: float = 2.6,
                  no_mode: bool = False, grab_images: bool = False) -> AskResult:
        """对外入口。除 _ask_inner 的结果外，统一附上弹窗清理记录。

        放在外层是因为 ask 内部有六七个提前 return 点，逐个塞字段迟早漏掉
        一个；这里一次收口，每条返回路径都会带上。
        """
        self.popup_report = []
        res = await self._ask_inner(page, prompt, reset=reset, timeout=timeout,
                                    stable_window=stable_window,
                                    no_mode=no_mode, grab_images=grab_images)
        if self.popup_report:
            tag = ",".join(self.popup_report)
            res.via = f"{res.via} [清弹窗:{tag}]" if res.via else f"[清弹窗:{tag}]"
        # 支持直连的站点：跑完浏览器路线后顺手把凭证收下来，
        # 供之后的 HTTP 路径使用。只在**本次成功**且**确实需要**时才收，
        # 免得每问一次都白写一遍文件。
        if res.ok and self.http_capable:
            try:
                from . import http_deepseek
                if http_deepseek.needs_harvest():
                    await self.harvest_credentials(page)
            except Exception:  # noqa: BLE001
                pass
        return res

    async def _ask_inner(self, page: Page, prompt: str, *, reset: bool = True,
                  timeout: float = 180.0, stable_window: float = 2.6,
                  no_mode: bool = False, grab_images: bool = False) -> AskResult:
        """no_mode=True 时跳过深度思考/模型切换。

        生图类请求必须用它：实测豆包把模型切到 2.1 Turbo 之后就只回文字
        不画图了 —— 生图能力挂在默认模型上，切走反而没了。

        grab_images=True 时，回答完之后再等图片渲染出来并下载到本地。
        这一步必须在**同一个调用里**做完：生成结果只存在于当前页面上下文，
        一旦浏览器被关（比如服务重启）就没了，事后再来抓是抓不到的。
        """
        t0 = time.time()
        # 每次提问前刷新一遍凭证存盘。会话 token 常有滑动有效期，
        # 一直用旧的反而更容易过期 —— 顺手更新成本极低（一次会话只有一个文件）。
        try:
            from . import browser
            await browser.manager.save_session(self.id)
        except Exception:
            pass
        via = ""
        try:
            if not await self.is_logged_in(page):
                return AskResult(False, self.id, error=f"{self.name} 未登录",
                                 elapsed=time.time() - t0)
            # ★ 进门先清场。更新日志、功能介绍这类浮层一旦压在输入框上，
            #   后面每一次点击都会卡到超时，而且报错信息完全看不出是弹窗的锅。
            self.popup_report += await self.dismiss_popups(page)
            baseline = "" if reset else await self.snapshot_answer(page)
            if reset:
                await self.new_chat(page)
                # 给页面一点时间真正切到新会话。不等的话，有些站点
                # （实测豆包）会把消息发进旧会话，于是它带着上一轮的图片
                # 上下文，把"生成一张新图"理解成"改这张图"。
                await page.wait_for_timeout(900)
                baseline = await self.snapshot_answer(page)
            if len(prompt.strip()) == 0:
                return AskResult(False, self.id, error="空提示词",
                                 elapsed=time.time() - t0)
            # ★ 抓图基线：把"**发问之前**页面上已经有的图"记下来。
            #   复用页面会话时（reset=False）历史消息里的旧图还挂在 DOM 上，
            #   不加这一步的话，下面 extract_images 会把它们当成这一轮的结果
            #   再下载一遍 —— 而且返回值看起来完全正常，是最难发现的那种错。
            #   limit 给大值是为了**收全**：漏一张就会把它误判成新图。
            img_baseline: set[str] = set()
            if grab_images:
                img_baseline = set(await self.extract_images(page, limit=500))
            slow = False
            if no_mode:
                self.mode_report = {"模式切换": "已跳过"}
            else:
                # 模式开关常在输入框上方的工具栏里，同样会被浮层压住
                if await self._has_popup(page):
                    self.popup_report += await self.dismiss_popups(page)
                slow = await self.apply_mode(page)
                if await self.apply_pick(page):
                    slow = True
            if self.ask_timeout:
                timeout = float(self.ask_timeout)      # 站点显式指定，优先
            elif slow:
                timeout = max(float(timeout or 0), self.slow_mode_timeout)
            full = (self.prompt_prefix + "\n\n" + prompt) if self.prompt_prefix else prompt
            # 最后一刻再兜一次：有些浮层是进页面几秒后才姗姗来迟的
            if await self._has_popup(page):
                self.popup_report += await self.dismiss_popups(page)
            via = await self.send(page, full)
            sw = self.stable_window or stable_window
            try:
                await self.wait_done(page, timeout=timeout,
                                     stable_window=sw, baseline=baseline)
            except TimeoutError:
                # 超时不直接作废：页面里多半已有大半答案，抓回来比扔掉强
                pass
            answer, evia = await self.extract(page)
            via = f"{via} → {evia}" if via else evia
            if is_agent_chatter(answer):
                return AskResult(
                    False, self.id,
                    error="抓到的是检索进展文本而非最终答案（Agent 模式未跑完）",
                    via=via, elapsed=time.time() - t0)
            # 抓回来的东西不可用时，助手其实还没答完。两种情况：
            #   1) 是提问回显 —— 站点先把用户消息渲染出来了；
            #   2) 只剩一条 UI 状态行（如"已完成思考，参考 6 篇资料"），正文还没吐。
            # 这里**轮询**等真答案出现，一拿到就走，不再等满 timeout。
            def _bad(a: str) -> bool:
                return (not a) or looks_like_echo(a, prompt) \
                    or not strip_noise(a).strip()

            if _bad(answer):
                polls, prev, same = 0, "", 0
                wait_deadline = time.time() + min(120.0, max(40.0, timeout * 0.5))
                while time.time() < wait_deadline:
                    await page.wait_for_timeout(2500)
                    polls += 1
                    a2, ev2 = await self.extract(page)
                    if not a2 or _bad(a2) or is_agent_chatter(a2):
                        prev, same = "", 0
                        continue
                    if a2 == prev:
                        same += 1
                        # 连续三次抓到同样的内容（约 7.5s 没变）才算完整，
                        # 否则只是流式输出里的半截文本
                        if same >= 2:
                            answer, evia = a2, ev2
                            via = f"{via} → {ev2}(重等{polls}次)"
                            break
                    else:
                        prev, same = a2, 0
                        answer = a2
                if _bad(answer):
                    if grab_images:
                        # ★ 生图轮例外：助手可能**只出图、不吐文字**，
                        #   这时「没抓到答案」是正常的，不能因此把已经画好的
                        #   图丢掉 —— 图才是这一轮的交付物。清空 answer
                        #   继续往下走，成败交给抓图段判定。
                        answer = ""
                    else:
                        return AskResult(
                            False, self.id,
                            error="助手回复未出现（页面可能仍在联网搜索/生成，或答案没抓到）",
                            via=via, elapsed=time.time() - t0)
            # 续聊时把上一轮的旧答案去掉，只留本次新增部分
            if not reset and baseline and answer.startswith(baseline):
                answer = answer[len(baseline):].lstrip("\n")
            if not answer and not grab_images:
                return AskResult(False, self.id, error="抓取到空答案", via=via,
                                 elapsed=time.time() - t0)
            # 生图：文字先出来，图还在画，所以要在返回前把它等出来并下载。
            images: list[str] = []
            if grab_images:
                img_deadline = time.time() + 75
                urls = await self.extract_images(page, exclude=img_baseline)
                while not urls and time.time() < img_deadline:
                    await page.wait_for_timeout(2500)
                    urls = await self.extract_images(page, exclude=img_baseline)
                if urls:
                    images = await self.download_images(
                        page, urls, referer=(self.homepage or self.url))
                    via = f"{via} +{len(images)}图"
                elif img_baseline:
                    # 页面上有图，但**全是发问之前就在的旧图** —— 也就是这轮
                    # 根本没生成新图。以前这种情况会静默返回 images=[]，
                    # 调用方以为"这轮没图"却不知道原因；现在写进 via 里能看见。
                    via = f"{via} 无新图(页面仅剩 {len(img_baseline)} 张旧图)"
                else:
                    via = f"{via} 无图"
            if grab_images and not answer and not images:
                # 生图轮把「没答案」放行了，但图和文字总得有一个，
                # 否则不能假装成功。
                return AskResult(False, self.id,
                                 error="这轮既没抓到文字回答，也没抓到新图",
                                 via=via, elapsed=time.time() - t0)
            return AskResult(True, self.id, answer=answer,
                             elapsed=time.time() - t0, via=via,
                             mode=dict(self.mode_report), images=images)
        except Exception as e:  # noqa: BLE001
            return AskResult(False, self.id, error=f"{type(e).__name__}: {e}",
                             elapsed=time.time() - t0, via=via)


# 深度思考模式的推理过程特征：它在"复述对话/自我推演"，
# 因此里面也会出现用户的提问，但它绝不是"提问被回显"
_REASONING_MARKERS = (
    "我们需要回答用户", "需要回答用户", "对话：", "用户问", "助手答",
    "现在需要回复", "最终只输出", "我需要基于知识库", "先回忆", "让我想想",
)


def looks_like_echo(answer: str, prompt: str) -> bool:
    """抓到的"答案"其实只是提问回显吗？

    有些站点（实测豆包）在发出提问后会立刻把用户消息渲染出来，
    而助手回复要等几秒（若开了联网搜索甚至要 15s+）。等待逻辑会把
    "用户消息出现"误判成"开始回答了"，于是收工并抓回一段提问原文。
    这里做一次判断，命中就说明还得接着等。
    """
    def norm(s: str) -> str:
        s = re.sub(r"[\s\u3000]+", "", s or "")
        s = re.sub(r"\d{1,2}:\d{2}", "", s)      # 去掉时间戳
        return s

    a, p = norm(answer), norm(prompt)
    if len(a) < 10 or len(p) < 10:
        return False
    # ★ 先过两道「铁证」，它们必须排在下面的推理过程豁免**之前**：
    #   ① 归一化后一字不差 → 抓到的就是我方刚发出去的那段文本；
    #   ② 抓到的东西以原提问开头 → 这是把用户消息也包进去的容器。
    #
    #   为什么顺序这么要紧：_REASONING_MARKERS 里含 "用户问"、"助手答"、
    #   "对话："，而回填给网页端的历史块曾经也写着 "用户问：…"
    #   （见 pool._build_context）。两边文案一撞，回填过的请求一旦把用户
    #   消息抓好，豁免分支就会**先把回显判定关掉** —— 那轮的"答案"于是成了
    #   我们自己发出去的 3618 字，还被写进话题历史、下一轮又回填下去，
    #   越滚越大（实测 3618 → 6173 字）。真机事故：2026-10-01 豆包生图轮
    #   （助手只出图、文字回答迟迟不来时必现）。
    if a == p:
        return True
    # 比提问长出一大截的不可能是"提问回显" —— 那通常是深度思考的推理过程
    # （它会把用户的提问复述一遍，容易被误判成回显而触发无谓的重试）。
    if len(a) > len(p) * 2 + 40:
        return False
    # ② 抓到的东西以原提问开头 → 这是把用户消息也包进去的容器。
    if a[:60] and a[:60] in p:
        return True
    # 深度思考的推理过程有明显特征（在复述对话），也不是回显
    if any(m in a for m in _REASONING_MARKERS):
        return False
    # 抓回来的东西里包含（大段）原提问 → 基本可以确定抓错了
    probe = p[:60]
    if probe and probe in a:
        return True
    return False


def looks_like_login_wall(html_text: str) -> bool:
    if not html_text:
        return False
    pats = ("扫码登录", "请登录", "登录后使用", "sign in to continue", "log in to continue")
    low = html_text.lower()
    return any(p.lower() in low for p in pats)


# 三重约束，尽量只在"它就是角标"时才动手：
#   连续两组以上  —— 避开 -40~85、9.6-115.2 这类正常范围/连字符写法
#   前面不是字母数字 —— 避开 P1-2-3、GPIO-2-3 这类编号
# 单组 -13 有可能是范围的一部分，宁可留下。
# 引用角标：DeepSeek 这类站点会给每句话挂来源编号，形如 "…致敏-11"、
# "…反应-2-5。"。特征很明确 —— **紧跟在中文/右括号后面**。
# 加这个前置约束后，P1-2-3、2024-2025、-40~85 这些都不会被误伤。
_REF_TAIL_RE = re.compile(
    # 末尾多加了 % ：占位（原来只认中文/闭合括号），所以"升学率达21.03%-11。"
    # 这种角标会漏 —— 它的角标挂在百分号后面。
    # 不放宽到"数字"是有意的：那样会把日期 2026-09-30 和型号给打散。
    r"(?<=[\u4e00-\u9fa5）】》”\"%])\s*(?:-\s*\d{1,3})+(?=[\s，。、；：！？）】]|$)")
# 数字已被剥掉、只剩一条孤零零短横的（"…致敏-。"）
_REF_DASH_RE = re.compile(
    r"(?<=[\u4e00-\u9fa5）】》”\"])\s*-(?=[\s，。、；：！？）】]|$)")
# 表格里那种脱离中文语境的： "最高 35 MHz -2-5 |"
# 连续两组以上、且前面不是字母数字（P1-2-3 这种引脚编号因此不受影响）
_REF_MULTI_RE = re.compile(r"(?<![0-9A-Za-z])(?:\s*-\d{1,3}){2,}")

# DeepSeek 走 API 直连时，角标是**方括号**形态（"…体感偏凉[reference:0]。"），
# 和网页端渲染出来的 "-11" 完全不是一套写法。两种都得认 ——
# 只认后者的话，直连拿回来的答案里会挂一串 [reference:N] 噪声。
_REF_BRACKET_RE = re.compile(
    r"\[\s*(?:reference|citation)\s*[:：]?\s*\d*\s*\]", re.I)


def strip_reference_marks(text: str) -> str:
    """去掉 "…致敏-11"、"…反应-2-5。" 里的来源角标。

    注意 DeepSeek 的角标渲染出来是 "-\\n20"（短横与数字隔一个换行），
    所以规则里的 \\s* 不能省；摘掉之后会在原地留下一个孤立换行
    （"研究生）\\n。"），最后一条副带把这个尾巴收干净。
    """
    if not text:
        return text
    text = _REF_BRACKET_RE.sub("", text)
    text = _REF_TAIL_RE.sub("", text)
    text = _REF_MULTI_RE.sub("", text)
    text = _REF_DASH_RE.sub("", text)
    # 角标被摘掉后留下的孤立换行：换行紧跟句读标点时合并掉。
    # 只认 \\n（不认空格），正常段落里的换行后面跟的是文字不是标点，不会被波及。
    return re.sub(r"\n+([，。、；：！？）】])", r"\1", text)


# 联网检索类站点会在正文前后挂一条状态行，例如
#   豆包："已完成思考，参考 6 篇资料"  /  "搜索 1 个关键词，参考 6 篇资料"
#   元宝："已搜索 3 个网页"
#   DeepSeek（检索类回答会带上）："搜索到 10 个网页"
_SEARCH_STATUS_RE = re.compile(
    r"^(?:已(?:完成)?(?:深度)?思考|正在思考|搜索(?:了)?\s*\d+\s*个?关键词"
    r"|已搜索\s*\d+\s*个?(?:网页|结果|关键词)?|正在搜索"
    r"|搜索到\s*\d+\s*个?(?:网页|结果|资料))"
    r"[，,、]?\s*(?:参考\s*\d+\s*篇?(?:资料|网页|结果|文章))?[，,、]?\s*"
)


# 站点工具栏的标签会跟着正文一起被抓进来（实测 Kimi 会带出"表格  复制"）
_UI_TAG_RE = re.compile(r"[ \t]*(?:表格|代码|图片|大纲|幻灯片)[ \t]+复制[ \t]*(?=\n|$)")


# DeepSeek 有时会在答案末尾粘一句固定免责声明（"本回答由 AI 生成，内容仅供参考，
# 请仔细甄别"），而且**不带换行**地直接贴死在最后一句后面 ——
# 所以不能靠"整行丢弃"处理，只能按结尾锚点剥。
_AI_DISCLAIMER_RE = re.compile(
    r"[\s\u3000]*(?:本(?:回答|答案|文|内容))?由\s*[Aa][Ii]\s*生成"
    r"[，,、]?\s*内容仅供参考[，,、]?\s*请[^\n。！？]{0,6}甄别[。！!]?[\s\u3000]*$")


def strip_noise(text: str) -> str:
    """去掉网页端常见的 UI 尾巴（"复制"、"重新生成"、时间戳等）。"""
    text = strip_reference_marks(text)
    text = _UI_TAG_RE.sub("", text)
    # 正文最前面的检索状态行（常与正文连在一起，所以按前缀剥离）
    text = _SEARCH_STATUS_RE.sub("", (text or "").lstrip(), count=1)
    lines = [ln.rstrip() for ln in text.splitlines()]
    drop = {"复制", "重新生成", "复制回答", "赞", "踩", "分享", "再试一次"}
    out = []
    for ln in lines:
        if ln.strip() in drop:
            continue
        # 单独成行的状态行也丢掉
        if ln.strip() and _SEARCH_STATUS_RE.fullmatch(ln.strip()):
            continue
        out.append(ln)
    text = "\n".join(out).strip()
    # 免责声明放在按行处理**之后**：它黏在最后一句后面，按行丢不掉
    return _AI_DISCLAIMER_RE.sub("", text).rstrip()

