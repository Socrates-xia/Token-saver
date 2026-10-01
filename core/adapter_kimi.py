from .adapter import BaseAdapter, Selectors


class Kimi(BaseAdapter):
    id = "kimi"
    name = "Kimi"
    url = "https://www.kimi.com"
    homepage = "https://www.kimi.com"
    tags = ["zh", "long-context", "summary", "document"]
    badge = "长文优势"
    note = "长文档摘要、综述整理这类任务性价比高。新版 kimi.com 偶尔会把提问当成 Agent 任务执行，引导词可降低概率。"
    # 新版 kimi.com 默认 K3 会把提问当 Agent 任务跑（跑失败返回"权益已退还"）。
    # 用一句引导词把它按回普通对话模式。
    prompt_prefix = "（请以普通对话方式直接回答下面的问题，不要创建任务、不要联网搜索、不要分步执行）"
    # 实测（2026-09-30）：Kimi 没有独立的"深度思考"开关，但**输入框右下角**
    # 有个模型选择器 [data-testid='model-select-trigger']（显示"快速 · 进阶"），
    # 点开后能选：
    #     快速 → 快速对话，即时响应
    #     K2.8 Preview → 性能与效果均衡
    #     K3 → 擅长对话与 Agent 任务，全能旗舰   ★ 最强
    # 另外还有个二级项「思考强度」（当前是"进阶"）。
    # 这就是用户说的"进阶模式，可以选 K3"。要更强推理就切 K3。
    #
    # 别去点「深度研究」——那是联网跑多步的 Agent 模式，只会吐检索计划。
    mode_pick = {"[data-testid='model-select-trigger']": "K3"}
    slow_mode_timeout = 300.0

    selectors = Selectors(
        input=[
            "div[contenteditable='true']",
            "textarea",
        ],
        answer=[
            # 实测正文容器的 class 就是 "markdown"，不含 "markdown-body"，
            # 原来只写 markdown-body 根本匹配不上。
            ".markdown",
            "[class*='markdown']",
            # 兜底：段落容器。但它在 K3 模式下也会套住推理过程
            # （toolcall-rollup 里那段英文 thinking），所以排最后。
            "[class*='paragraph'] .container",
        ],
    )
