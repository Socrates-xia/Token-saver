from .adapter import BaseAdapter, Selectors


class Yuanbao(BaseAdapter):
    id = "yuanbao"
    name = "腾讯元宝"
    url = "https://yuanbao.tencent.com/chat/"
    homepage = "https://yuanbao.tencent.com"
    tags = ["zh", "general", "summary", "translation"]
    badge = "国内直连"
    note = "中文摘要、翻译、润色成本低，适合日常杂活。"

    # 元宝没有"深度思考"开关，而是把思考强度做进了模型下拉：
    # 点当前模型按钮 → 菜单里选"深度思考（深入推理，分析复杂问题）"。
    # 注意别选"专家模式"——那是会联网跑多步的 Agent 模式。
    #
    # 坑1：不要用 aria-label="切换模型" 定位 —— 那个属性挂在按钮上但点不动。
    # 坑2：按钮可见文字随选中项变化（快速回答/深度思考/专家模式），不能写死。
    # 坑3：菜单项是 <button aria-checked>，没有 role=menuitem。
    # 坑4：菜单渲染要 >1s，等太短会误判"没打开"。
    # → 用元宝自带的测试属性定位，最不受文案改版影响。
    mode_pick = {"[data-thinking-mode-switcher-trigger='true']": "深度思考"}
    slow_mode_timeout = 240.0
    # 深度思考模式下思考过程会先流式刷一遍，之后才出正文；
    # 稳定窗口放宽一点，避免把"思考刚结束"的瞬间当成回答完成。
    stable_window = 5.0

    selectors = Selectors(
        input=[
            "div[contenteditable='true']",
            "textarea",
        ],
        new_chat=[
            "[class*='new-chat']",
            "text=新建对话",
        ],
        answer=[
            # 元宝的"思考过程"和"正文答案"共用同一个 markdown 类名
            # （正文 = div.hyc-common-markdown，思考面板 = div.hyc-component-deepsearch-cot）。
            #
            # ★ 踩过的坑：最早写成
            #     div[class*='markdown']:not(.hyc-component-deepsearch-cot *)
            #   想用 CSS4 的复杂否定一句话排掉思考面板。实测**命中 0 个元素** ——
            #   本机 Chromium 对这种 `:not()` 里的后代组合选择器不支持，
            #   而且不报错。于是这个"首选选择器"静默失效，一路落到泛选择器
            #   [class*='markdown'] 上，思考过程照样被抓回来。
            #   换成 :not(.hyc-component-deepsearch-cot) 能命中，但那只排除
            #   元素自身，排除不了它的子孙，思考面板里的 markdown 照样漏进来。
            # → 正解：选择器只管"命中哪些类名"，"哪些不算答案"交给
            #   answer_exclude 用 JS 的 closest() 判断（见 base.answer_exclude）。
            "div[class*='hyc-common-markdown']",
            "[class*='answer-content']",
        ],
    )

    # 思考面板内部的一切都不是答案。
    #
    # ★ 排除的必须是 __think 这一层，**不能是整个 .hyc-component-deepsearch-cot**：
    #   实测该容器是"整条 AI 消息"的外壳，思考块和正文答案是它的两个子块。
    #   排掉整壳 = 把答案一起排掉，snapshot_answer() 直接返回空字符串，
    #   外层等答案等到超时（实测 600 秒不返回）。
    #   判据：外壳 innerText 长度 ≈ 思考 + 答案 + 14
    #   （1390 + 1058 = 2448，实测 2462），三者是包含关系。
    answer_exclude = [".hyc-component-deepsearch-cot__think"]

    # 元宝会往答案里塞"相关视频"推荐卡片，而且**和正文同属一个
    # .hyc-common-markdown 容器**（层级是
    #   .hyc-common-markdown > .ybc-p > .ybc-chat-videoBoxV2-bigCard-wrapper
    #     > .ybc-chat-videoBoxV2-bigCard__title），所以换选择器躲不开，
    # 只能把这块子树摘掉。实测不摘的话答案尾巴会挂上：
    #   "相关视频00:44每天一分钟舒尔特方格练习
    #    #专注力#专注力训练#亲子互动#舒尔特#舒尔特训练
    #    艾袒心ADHD成长营2周前"
    # 注意：站点"复制"按钮给的 Markdown 原文里没有这个卡片，
    # 所以走剪贴板那条路时不受影响 —— 只有 DOM 路线需要这份名单。
    answer_strip = [
        "[class*='videoBox']",       # ybc-chat-videoBoxV2-bigCard-wrapper / __title
        "[class*='video-box']",      # 同上：同一元素的第二个类名
        "[class*='relatedQuestion' i]",   # 猜你想问
    ]
