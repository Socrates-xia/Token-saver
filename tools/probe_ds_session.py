"""观察 DeepSeek 连续两条消息到底落在哪个会话里。

用法（需先停掉 server.py）：
    python tools/probe_ds_session.py
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core import browser  # noqa: E402
from core.adapter_deepseek import DeepSeek  # noqa: E402


async def say(a, page, text, wait=90):
    inp, via = await a.find_input(page)
    if inp is None:
        print("  !! 找不到输入框")
        return ""
    await a._type(page, inp, text)
    await page.wait_for_timeout(400)
    u_before = page.url
    await a.send(page, "")
    try:
        await a.wait_done(page, timeout=wait, stable_window=4.0)
    except TimeoutError:
        print("  (等待超时，抓当前内容)")
    await page.wait_for_timeout(1500)
    ans, evia = await a.extract(page)
    print(f"  发送前 URL: {u_before}")
    print(f"  发送后 URL: {page.url}")
    print(f"  URL 变了吗: {u_before != page.url}")
    print(f"  答案({evia}): {ans[:60]!r}")
    return ans


async def main() -> None:
    a = DeepSeek()
    page = await browser.manager.ensure_page(a)
    await page.wait_for_timeout(2500)
    print("初始 URL:", page.url)

    print("\n--- 新建对话 ---")
    await a.new_chat(page)
    await page.wait_for_timeout(1500)
    print("new_chat 后 URL:", page.url)

    # ★ 关键：API 路径会在每次提问前调 apply_mode（点"深度思考"开关）。
    # 这里也加上，看它是不是破坏会话的元凶。
    print("\n--- apply_mode（开深度思考）---")
    slow = await a.apply_mode(page)
    print("apply_mode →", slow, a.mode_report, "| URL:", page.url)

    print("\n--- 第 1 条 ---")
    await say(a, page, "请记住数字 77。只回复：好的")
    url_a = page.url

    print("\n--- 第 2 条前再调一次 apply_mode（模拟 API 路径）---")
    slow = await a.apply_mode(page)
    print("apply_mode →", slow, a.mode_report, "| URL:", page.url)

    print("\n--- 第 2 条（不新建会话，模拟追问）---")
    await say(a, page, "我让你记住的数字是多少？只回复数字本身。")
    url_b = page.url

    print("\n--- 第 3 条（再追问）---")
    await say(a, page, "把这个数字加 1，只回复结果。")
    url_c = page.url

    print("\n===== 汇总 =====")
    print("第1条后 URL:", url_a)
    print("第2条后 URL:", url_b, "| 与第1条相同:", url_a == url_b)
    print("第3条后 URL:", url_c, "| 与第2条相同:", url_b == url_c)

    await browser.screenshot(page, "deepseek", "session")
    await browser.manager.close_all()


asyncio.run(main())
