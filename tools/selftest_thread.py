"""离线回归测试：话题历史 / 回填 / 上下文自愈几个判定函数。

    python tools/selftest_thread.py

不联网、不开浏览器、不消耗账号额度。

为什么要专门测这几个函数：它们的失败方式都是**静默**的 ——
判错不会报错、不会崩，只会悄悄多烧一次调用（误报跑题 → 白重试一遍）
或者悄悄交付一个答错对象的答案（漏报），再或者把不该回填的历史发出去。
属于"不测就一定会烂掉"的那类代码。

其中的长上文用例来自 2026-10-01 的一次真机踩坑：一次完全接上上文的追问
被判成跑题、白扔了一整次调用。细节见 core/pool.py 里 _DRIFT_PREV_MAX 的注释。

隐式话题那几项来自同一天的用户实测反馈："新开豆包生图，发出去的提示词里
带着很久前的对话"—— 元凶是 auto-<站点> 这个隐式话题把毫不相干的任务
全粘在一起、又在网页会话没接上时整段回填。见 _history_of 的说明。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.adapter import _REASONING_MARKERS  # noqa: E402
from core.pool import (  # noqa: E402
    _build_context,
    _history_of,
    _is_implicit_thread,
    _looks_like_missing_ctx,
    _looks_like_topic_drift,
)

fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {name}")
    if not ok:
        print(f"      期望 {want!r}")
        print(f"      实际 {got!r}")
        fails.append(name)


# --------------------------------------------------------------- 素材
# 短上文：一句天气（真实形态，约 60 字）
WEATHER = ("永城市今天（10月1日）天气以阴天为主，夜间转多云，"
           "气温13℃至19℃，东北风微风，空气质量优。")

# 长上文：营养类长回答（真实形态，markdown 密集、>600 字）
LONG_SOUP = """**常喝汤是否健康，关键取决于汤的种类、熬制方法和饮用频率。**

### 常喝汤的潜在好处

**1. 增加饱腹感，辅助体重管理**
汤的高含水量能提供较大的食物体积，从而在物理上填充胃部，增加饱腹感。

**2. 补充水分与部分营养素**
汤是补充水分的良好方式，如果汤中富含蔬菜，可以摄入蔬菜中的矿物质。

### 常喝汤的潜在风险

风险主要集中在以肉类、骨头为原料，且经过长时间熬煮的"浓汤"上。

**1. 高钠** —— 汤是"隐形盐"的重灾区。长期摄入过量钠会显著增加高血压风险。

**2. 高嘌呤** —— 嘌呤极易溶于水，长时间炖煮过程中嘌呤会大量释放到汤里。
对于高尿酸血症和痛风患者，喝汤可能比吃肉摄入更多嘌呤。

**3. 高脂肪** —— 长时间熬煮会使脂肪乳化，形成奶白色的浓郁外观。

### 如何更健康地喝汤？

综合来看，如果你想将汤纳入日常饮食，可以参考以下原则：

*   **控制频率与分量**：不要顿顿喝汤，尤其是浓汤。每餐喝小半碗即可。
*   **缩短熬煮时间**：对于肉汤，建议熬煮时间控制在1小时左右，最长不超过2小时。
*   **选择清淡汤底**：优先选择以蔬菜、蛋花、豆腐等为食材的快煮清汤。
*   **自制并控盐**：自制汤品可以更好地控制盐分，起锅前再放盐。
*   **喝汤也吃肉**：汤中溶解的营养有限，大部分蛋白质仍留在肉里。
*   **注意温度**：将汤盛出后稍微晾凉，待温度降到不烫嘴时再喝。

**总结**：汤本身并非"健康"或"不健康"的绝对标签，其健康效应完全取决于
你喝的是什么汤、怎么喝。清淡的蔬菜汤可以作为补充水分和蔬菜摄入的有益补充；
而频繁饮用高盐、高脂、高嘌呤的浓汤或老火汤，则会对心血管、肾脏和代谢健康
构成明确风险。

### 为了给你更具体的建议，可以告诉我吗？

1. 你主要喝的是哪种汤？（例如：蔬菜汤、排骨汤、鸡汤、鱼汤等）
2. 你是否有高血压、高尿酸或痛风、高血脂等需要关注的健康问题？
"""

# 换了一套词的正常追问回答（不含上文的高频话题词，但确实接上了上文）
SOUP_FOLLOWUP = ("你提到的这两个方法确实很关键。焯水主要降低嘌呤："
                 "预热处理能让肉中的部分嘌呤提前溶出并被弃掉。"
                 "冷藏撇油则是去除油脂最有效的方法。")

# 答错对象的回答：问的是永城，通篇在讲睢县
DRIFTED = ("睢县值得去的地方不少。北湖景区可以环湖骑行，"
           "还有睢杞战役纪念馆、袁家山、承匡城遗址等。")

# 接对了的回答
ON_TOPIC = ("永城值得去的地方不少。芒砀山汉文化景区是核心，"
            "还有陈官庄纪念馆、日月湖、崇法寺塔等。")


print("=== 跑题检查：短上文（这套判据真正适用的场景）===")
check("短上文 + 指代词 + 答错对象 → 判跑题",
      _looks_like_topic_drift(DRIFTED, WEATHER, "那边有什么好玩的地方？"),
      True)
check("短上文 + 指代词 + 答对对象 → 不判跑题",
      _looks_like_topic_drift(ON_TOPIC, WEATHER, "那边有什么好玩的地方？"),
      False)
check("短上文但提问没用指代词 → 不判跑题",
      _looks_like_topic_drift(DRIFTED, WEATHER, "睢县有什么好玩的地方？"),
      False)

print()
print("=== 跑题检查：长上文（★ 真机误报的那一类）===")
check("长上文 + 换词追问 → 不判跑题（别误伤）",
      _looks_like_topic_drift(SOUP_FOLLOWUP, LONG_SOUP,
                              "你前面提到缩短熬煮时间，那焯水到底有没有用？"),
      False)
check("长上文超过阈值 → 直接不做这项检查",
      len(LONG_SOUP) > 600, True)
# ★ 真机第二次踩到：调用方传进来的其实是"上一轮答案的末尾 60 字"
#   （那是给浏览器路线比对页面残留用的片段），60 < 阈值，长度保护形同虚设，
#   长上文照样被硬判成跑题 → 又白扔一整套"新会话 + 回填历史"。
#   函数自己分辨不出"这不是完整上文"，责任在调用方，所以这里直接锁死契约：
#   pool.ask 必须把**全文**喂进去。
POOL_SRC = (ROOT / "core" / "pool.py").read_text(encoding="utf-8")
check("pool.ask 交给跑题检查的是全文（prev_full），不是尾部片段",
      "prev_full, prompt" in POOL_SRC and "prev_answer, prompt" not in POOL_SRC,
      True)

print()
print("=== 跑题检查：不该被噪声骗到 ===")
# 上文里塞满 markdown，特征词若取自原文就会变成 "。\n\n" / "\n**" 这类残渣。
# 剥成纯中文后，"永城" 这种真话题词才浮得出来。
check("markdown 密集的短上文也能抓到真话题词",
      _looks_like_topic_drift(DRIFTED, "**永城市**今日：\n\n* 阴天\n* 13℃\n",
                              "那边呢？"),
      True)

print()
print("=== 反问检查（上下文真的没接上）===")
check("反问已经交代过的信息 → 命中",
      _looks_like_missing_ctx("你说的'那边'具体是指哪里呀？"), True)
check("正常回答 → 不命中",
      _looks_like_missing_ctx("永城在河南省最东部，芒砀山值得一去。"), False)
check("过长文本不做此判断（长答案几乎不会反问）",
      _looks_like_missing_ctx("具体是指哪里。" + "字" * 700), False)

print()
print("=== 回填块：文案不许撞上推理特征词（★ 2026-10-01 豆包生图事故）===")
# 事故链条：_build_context 里每轮拼的是"用户问：… / 你的回答：…"，
# 而 adapter._REASONING_MARKERS 里也含"用户问"（那张表本是给"深度思考在
# 复述对话"用的豁免名单）。回填过的请求 → looks_like_echo 的豁免分支先返回
# False → 回显判定被关掉 → 抓回来的我方消息当成答案、还写进话题历史、
# 下一轮又回填下去（实测 3618 → 6173 字）。
HIST = [
    {"q": "请系统地列出中国的所有主要节日，包括传统节日和法定节假日。",
     "a": "以下系统梳理中国的主要节日，分为传统节日、法定节假日等四大类。"},
    {"q": "画一只奶龙，卡通风格", "a": "我会把它处理成圆润卡通形象，线条简洁。"},
]
CTX = _build_context(HIST)
check("回填块里不含任何推理特征词（含'用户问'）",
      [m for m in _REASONING_MARKERS if m in CTX], [])
check("回填块确实把历史和本轮问题都包住了",
      ("中国的所有主要节日" in CTX) and CTX.rstrip().endswith("新问题）"), True)
check("没有历史时不产生任何回填块", _build_context([]), "")

print()
print("=== 隐式话题（auto-<站点>）：只借网页会话，不借历史 ===")
# 这是"新开豆包生图却带着很久前对话"的元凶：auto-doubao 会把同一站点的
# 所有零散任务粘在一个话题里（实测 18 小时前的"列举中国节日"、
# 之后的翻译、再之后的生图），一旦回填就整段发出去。
AUTO = {"provider": "doubao", "chat_url": "https://www.doubao.com/chat/1",
        "messages": HIST}
NAMED = {"provider": "yuanbao",
         "chat_url": "https://yuanbao.tencent.com/chat/x", "messages": HIST}
check("auto-<站点> 的历史一律不可回填", _history_of("auto-doubao", AUTO), [])
check("点名话题的历史照旧可以回填（关窗自愈要用）",
      _history_of("yuanbao-关窗测试", NAMED), HIST)
check("没给话题 → 没有历史可用", _history_of("", NAMED), [])
check("话题不存在 → 没有历史可用", _history_of("x", None), [])
check("识别隐式话题：auto-doubao", _is_implicit_thread("auto-doubao"), True)
check("识别隐式话题：点名话题不算", _is_implicit_thread("yuanbao-关窗测试"), False)
check("识别隐式话题：空话题不算", _is_implicit_thread(""), False)

# 源码契约：光有 _history_of 还不够，下面两条是实现里的关键闸门，
# 哪天有人"顺手简化"就会把问题带回来。
check("隐式话题不再往话题里记内容",
      "if not _is_implicit_thread(tid):" in POOL_SRC, True)
check("隐式话题 + 网页会话没接上 → 干净开局（不回填也不发进旧会话）",
      "if implicit and not live:" in POOL_SRC, True)
check("交付前有最后一道闸（不许把我方发出去的文本当答案）",
      "抓回来的是我们自己发出去" in POOL_SRC, True)

print()
if fails:
    print(f"✗ {len(fails)} 项失败：{'、'.join(fails)}")
    raise SystemExit(1)
print("✓ 全部通过")
