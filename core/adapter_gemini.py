from .adapter import BaseAdapter, Selectors


class Gemini(BaseAdapter):
    id = "gemini"
    name = "Google Gemini"
    url = "https://gemini.google.com/app"
    homepage = "https://gemini.google.com"
    tags = ["en", "general", "multimodal", "summary"]
    badge = "需代理"
    note = "英文与多模态任务强；国内需配合代理使用。"

    # Gemini 网页端没有思考强度开关：2.5 Flash 默认就在思考，要更强的推理
    # 得在模型下拉里换 Pro（那是模型选择，不是开关）。故此处不配。
    # 想强制用 Pro 的话，可在 config.yaml 里加 picks: {"": "2.5 Pro"} 之类。

    selectors = Selectors(
        input=[
            "rich-textarea div[contenteditable='true']",
            "div[contenteditable='true']",
            "textarea",
        ],
        send_button=[
            "button[aria-label*='Send' i]",
            "button.send-button-container",
        ],
        answer=[
            ".model-response-text",
            "[class*='model-response']",
            "message-content",
        ],
        stop_button=[
            "button[aria-label='Stop receiving messages']",
        ],
    )
