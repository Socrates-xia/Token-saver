from .adapter import BaseAdapter, Selectors


class DeepSeek(BaseAdapter):
    id = "deepseek"
    name = "DeepSeek 网页版"
    url = "https://chat.deepseek.com"
    homepage = "https://chat.deepseek.com"
    tags = ["code", "math", "reasoning", "zh"]
    badge = "免费额度充足"
    note = "代码与数学较强，国内直连无需代理。"

    # 开启深度思考（R1 推理）。联网搜索按钮站点默认是开的，这里不动它。
    # 在 config.yaml 里写 providers.deepseek.toggles.深度思考: false 即可关闭。
    mode_toggles = {"深度思考": True}
    # 深度思考正常 30~60s。原来设 300s 太宽松：万一判据失准会白等 5 分钟，
    # 用户那边看到的是"明明答完了程序却卡着"。180s 足够，超了还能抓现有内容。
    slow_mode_timeout = 180.0

    # 支持纯 HTTP 直连（PoW 已打通，见 core/pow_deepseek.py）。
    # 开了之后首字延迟从 7~15s 降到 1~2s，盈亏平衡点随之下压。
    http_capable = True
    http_timeout = 180.0

    selectors = Selectors(
        input=[
            "textarea#chat-input",
            "textarea[placeholder*='DeepSeek' i]",
            "textarea",
        ],
        popup_close=[
            # DeepSeek 进场常弹「更新日志 / 新功能介绍」，容器带 ds-* 前缀。
            # 通用启发式已经能兜住大部分，这里只是给个更准的捷径；
            # 站点改版后失效也没关系，会回落到 Esc 和通用扫描。
            "[class*='ds-dialog'] [class*='close' i]",
            "[class*='ds-modal'] [class*='close' i]",
            "div[class*='ds-consent'] button",
            "div[class*='ds-dialog'] button[aria-label*='close' i]",
        ],
        answer=[
            # ★ 优先取"真正的回答正文"。深度思考模式下页面上会有两个
            #   .ds-markdown：一个是**思考过程**（里面会复述用户的提问），
            #   一个是答案正文。只写 .ds-markdown 时，通用兜底容易先抓到
            #   思考过程，然后被"回声检测"误判成提问回显、一路空等到超时。
            ".ds-assistant-message-main-content",
            ".ds-markdown",
            "[class*='ds-markdown']",
        ],
        stop_button=[
            "div[class*='ds-stop']",
            "[class*='stop-generating']",
        ],
    )

    # ------------------------------------------------------------ 纯 HTTP
    async def harvest_credentials(self, page) -> None:
        """浏览器路线跑通后，顺手把 Cookie + Bearer 收下来给 HTTP 用。

        失败不影响主流程 —— 大不了下次继续走浏览器。
        """
        try:
            from . import http_deepseek
        except Exception:  # noqa: BLE001
            return
        try:
            await http_deepseek.harvest(page)
        except Exception as e:  # noqa: BLE001
            print(f"[http] 收凭证失败（不影响本次回答）："
                  f"{type(e).__name__}: {e}", flush=True)

    async def ask_http(self, prompt: str, *, thread: str = "",
                       reset: bool = False, timeout: float | None = None,
                       no_mode: bool = False, grab_images: bool = False,
                       context: str = "", should_stop=None):
        """不开浏览器直接问。**任何失败都返回 None**，让 pool 退回浏览器路线。

        返回 None 是刻意的：HTTP 只是加速路径，不能因为它的失败
        把整个 provider 拖下水。
        """
        from . import http_deepseek
        from .adapter import AskResult

        # 生图类请求不走 HTTP：DeepSeek 没有生图能力，且 grab_images 的
        # 语义是"等页面把图画出来"，和这条路径完全不搭。
        if grab_images:
            return None
        if not http_deepseek.enabled(self.conf):
            return None

        thinking = bool(self.toggles.get("深度思考", True)) and not no_mode
        to = float(timeout or self.http_timeout)
        try:
            out = await http_deepseek.ask(
                prompt, thread=thread, reset=reset,
                thinking=thinking, search=True, timeout=to,
                context=context, should_stop=should_stop)
        except http_deepseek.HttpAuthError as e:
            print(f"[http] 凭证不可用 → 退回浏览器：{e}", flush=True)
            return None
        except http_deepseek.HttpStopped:
            # ★ 用户叫停 ≠ "这条路走不通"，**必须原样抛出去**。
            #   落进下面那个 except Exception 就会被记成
            #   "直连失败 → 退回浏览器"：日志撒谎，调用方还会傻乎乎
            #   去开浏览器（虽然停机闸会拦住，但白绕一圈）。
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[http] 直连失败 → 退回浏览器：{type(e).__name__}: {e}",
                  flush=True)
            return None

        ans = (out.get("answer") or "").strip()
        if not ans:
            print("[http] 没抓到正文 → 退回浏览器", flush=True)
            return None
        res = AskResult(
            True, self.id, answer=ans,
            elapsed=float(out.get("elapsed") or 0.0),
            via="http直连(无浏览器)",
            mode={"深度思考": "on" if thinking else "off"},
            session_id=out.get("session_id") or "",
            resumed=bool(out.get("resumed")),
        )
        return res
