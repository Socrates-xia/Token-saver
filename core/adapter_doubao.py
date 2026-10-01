from .adapter import BaseAdapter, Selectors


class Doubao(BaseAdapter):
    id = "doubao"
    name = "豆包"
    url = "https://www.doubao.com/chat/"
    homepage = "https://www.doubao.com"
    tags = ["zh", "general", "translation", "creative"]
    badge = "国内直连"
    note = "响应快，适合短问答、改写、起名等轻量任务。"

    # 实测（2026-09-30，登录态，截图见 data/shots/doubao-*-probe.png）：
    # 豆包当前版本【已经没有"深度思考"开关】。模型下拉里只有两项：
    #     豆包 快速   —— 默认，快问快答
    #     豆包 2.1 Turbo [专家] —— 更强推理
    # 所以这里退而求其次，自动切到最强的 2.1 Turbo。
    # 触发按钮：<div data-valid-btn="model-select-action-btn" aria-expanded>
    # （有 aria-expanded / data-state 可判断开合，比文字稳）。
    # 想让豆包保持默认快速模型，把下面这行的 picks 删掉即可。
    mode_pick = {"[data-valid-btn='model-select-action-btn']": "2.1 Turbo"}
    # 联网搜索 + 2.1 Turbo 通常 30~50s，但**追问**时会明显更慢：
    # 实测同一话题第 2 问 92s、第 3 问 188s（要重新联网检索）。
    # 原来给 90s 硬上限会导致超时后抓到上一轮的答案，所以放宽到 220s。
    # 想让它更快失败可以把这里调小，代价是追问容易超时。
    ask_timeout = 220.0
    stable_window = 5.0

    selectors = Selectors(
        input=[
            "div[contenteditable='true'][data-slate-editor='true']",
            "div[contenteditable='true']",
        ],
        # 豆包改版后正文容器的 class 全是 tailwind 工具类
        # （如 "flex flex-col flex-grow max-w-full min-w-0"），毫无辨识度，
        # 而 [class*='message-content'] 会同时命中"用户提问"气泡，实测因此
        # 把提问回显当成了答案。真正可靠的是它用 data-plugin-identifier
        # 标的块类型（实测 t=15s 时已能看到）：
        #     block_type:10025 | search_query_result_block → 联网搜索状态条
        #     block_type:10000                              → 正文块 ★
        # 只认正文块还有个额外好处：正文没吐出来时抓取结果为空，
        # 等待逻辑就会接着等，不会把"思考步骤/状态条"误当成答案收工。
        answer=[
            "[data-plugin-identifier^='block_type:10000']",
            "[data-testid='receive_message']",
            "[data-testid='message_content']",
        ],
    )
