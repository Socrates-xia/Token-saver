"""内置网页端适配器注册表。

新增一家 = 新建一个 `core/adapter_<id>.py` + 在这里注册。

> 为什么适配器不放在 `core/adapters/` 子包里（2026-10-01 改）
> 技能包的上架校验有一条硬约束：**包内文件最多两级**（根/一级目录/文件）。
> `core/adapters/deepseek.py` 是三级，会被平台拒收。所以拍平成
> `core/adapter_deepseek.py`，本模块（`core/adapters.py`）原样保留原来的
> 注册表语义，调用方 `from core import adapters as pkg` 一个字都不用改。
"""
from .adapter_chatgpt import ChatGPT
from .adapter_deepseek import DeepSeek
from .adapter_doubao import Doubao
from .adapter_gemini import Gemini
from .adapter_kimi import Kimi
from .adapter_tongyi import Tongyi
from .adapter_yuanbao import Yuanbao

ADAPTERS = [
    DeepSeek(),
    Yuanbao(),
    Doubao(),
    Kimi(),
    Tongyi(),
    Gemini(),
    ChatGPT(),
]

_BY_ID = {a.id: a for a in ADAPTERS}


def all_adapters() -> list:
    return ADAPTERS


def get(pid: str):
    return _BY_ID.get(pid)


def ids() -> list[str]:
    return list(_BY_ID.keys())


__all__ = ["ADAPTERS", "all_adapters", "get", "ids"]
