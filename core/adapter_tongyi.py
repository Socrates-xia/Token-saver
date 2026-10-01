from .adapter import BaseAdapter, Selectors


class Tongyi(BaseAdapter):
    id = "tongyi"
    name = "通义千问"
    url = "https://tongyi.aliyun.com/qianwen/"
    homepage = "https://tongyi.aliyun.com"
    tags = ["zh", "general", "translation", "office"]
    badge = "国内直连"
    note = "阿里系，日常问答与办公文案类够用。"

    # 通义有"深度思考"开关。注意：状态读不到时代码会选择不动它（报 unknown），
    # 避免把本来就开着的关掉 —— 未登录无法实测，登录后建议跑一次确认。
    mode_toggles = {"深度思考": True}
    slow_mode_timeout = 240.0

    selectors = Selectors(
        input=[
            "textarea",
            "div[contenteditable='true']",
        ],
        answer=[
            "[class*='markdown-body']",
            "[class*='answer-content']",
        ],
    )
