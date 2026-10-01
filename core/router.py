"""任务路由：判断该不该外包，以及外包给谁。

不是所有任务都适合丢给网页端。这里做两件事：
1. classify / score —— 估算任务是不是"简单活"，给出建议（外包 or 主模型留用）
2. rank —— 在已登录的 provider 里按标签匹配排序
"""
from __future__ import annotations

import re
from typing import Iterable

from . import settings

# 明确适合外包的任务类型（工具调用、格式化、翻译、摘要等）
_TASK_PATTERNS: list[tuple[str, str, int]] = [
    (r"翻译|translate|译文", "translation", 3),
    (r"摘要|总结一下|概括|summary|abstract", "summary", 3),
    (r"润色|改写|polish|rewrite|缩写|扩写", "rewrite", 3),
    (r"纠错|语法|typo|grammar", "polish", 3),
    (r"转成|格式化|format|提取成|写成 ?json|yaml", "format", 3),
    (r"起名|文案|标题|slogan", "creative", 2),
    (r"注释|comment|explain.*code|解释.*代码", "explain", 2),
    (r"什么意思|是什么|what is|定义", "qa", 2),
    (r"正则|regex", "code", 2),
    (r"单位换算|计算|convert", "math", 2),
]

_HEAVY = re.compile(
    r"重构|架构|系统设计|多文件|整仓|全量审阅|refactor|architect|"
    r"agentic|multi-file|端到端|根因分析|性能优化方案|完整实现",
    re.I,
)


def classify(prompt: str) -> tuple[str, int]:
    """返回 (任务类型, 该任务被命中的强度)。"""
    best, hit = "general", 0
    for pat, task, score in _TASK_PATTERNS:
        if re.search(pat, prompt, re.I):
            if score > hit:
                best, hit = task, score
    return best, hit


def complexity(prompt: str) -> dict:
    """给任务打复杂度分（0-100），越高越不建议外包。"""
    cfg = settings.get()["routing"]
    n = len(prompt)
    score = 0
    reasons: list[str] = []

    if n > cfg["simple_max_chars"]:
        score += 25
        reasons.append(f"提示词较长（{n} 字符）")

    heavy = [k for k in cfg["heavy_keywords"] if k.lower() in prompt.lower()]
    if heavy:
        score += 15 * len(heavy)
        reasons.append("含重构/推导类关键词：" + "、".join(heavy[:3]))

    if _HEAVY.search(prompt):
        score += 30
        reasons.append("涉及多文件或系统级改造")

    code_blocks = prompt.count("```")
    if code_blocks >= 2:
        score += 10 * (code_blocks // 2)
        reasons.append(f"含 {code_blocks // 2} 段代码块")

    task, hit = classify(prompt)
    if hit >= 3:
        score -= 20
        reasons.append(f"属于「{task}」类标准化任务，外包收益高")

    score = max(0, min(100, score))
    verdict = "outsource" if score <= 45 else (
        "borderline" if score <= 65 else "keep_local"
    )
    return {
        "score": score,
        "verdict": verdict,
        "task": task,
        "reasons": reasons,
        "suggestion": {
            "outsource": "建议丢给网页端免费额度处理",
            "borderline": "可尝试外包，结果回来后自己复核要点",
            "keep_local": "建议仍用主模型，外包容易返工",
        }[verdict],
    }


def rank(adapters: Iterable, availability: dict[str, bool], prompt: str = "") -> list:
    """把已登录且启用的 adapter 排序，越靠前越优先。"""
    task, hit = classify(prompt) if prompt else ("general", 0)
    cfg = settings.get()

    def key(a):
        conf = cfg.get("providers", {}).get(a.id, {})
        score = 0
        if availability.get(a.id):
            score += 100
        if task in a.tags:
            score += 30
        if conf.get("priority"):
            score += int(conf["priority"])
        if conf.get("enabled") is False:
            score -= 1000
        return -score

    return sorted(adapters, key=key)
