from .adapter import BaseAdapter, Selectors


class ChatGPT(BaseAdapter):
    id = "chatgpt"
    name = "ChatGPT 网页版"
    url = "https://chatgpt.com"
    homepage = "https://chatgpt.com"
    tags = ["en", "general", "code", "translation"]
    badge = "需代理"
    note = "英文与通用问答质量高；国内需配合代理使用。"

    # ChatGPT 在推理模型下有个 "Think longer" 切换（英文界面）。
    # 中文界面或版本变化时找不到就自动跳过，不会报错。
    mode_toggles = {"Think longer": True}
    slow_mode_timeout = 300.0

    selectors = Selectors(
        input=[
            "div#prompt-textarea",
            "div[contenteditable='true'][id='prompt-textarea']",
            "textarea#prompt-textarea",
        ],
        send_button=[
            "button[data-testid='send-button']",
            "[data-testid='composer-send-button']",
        ],
        stop_button=[
            "button[data-testid='stop-button']",
        ],
        answer=[
            "[data-message-author-role='assistant'] .markdown",
            "[data-message-author-role='assistant']",
        ],
        new_chat=[
            "a[aria-label*='New chat' i]",
            "[data-testid='create-new-conversation-button']",
        ],
        copy_button=[
            "button[data-testid*='copy']",
            "[data-testid='copy-turn-action-button']",
        ],
    )
