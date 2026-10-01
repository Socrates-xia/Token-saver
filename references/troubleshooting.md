# 排障决策树

按症状查。绝大多数问题都能在"看一眼页面上到底有什么"之后定位。

## 万能第一步：看页面上有什么

```bash
# 页面上有哪些"像答案"的容器、里面是什么
curl -s -X POST http://127.0.0.1:8787/api/debug/dom \
  -H "Content-Type: application/json" -d '{"provider":"deepseek","kind":"answers"}'

# 我们的抓取函数当前拿到了什么（排"抓错东西"最有效）
curl -s -X POST http://127.0.0.1:8787/api/debug/dom \
  -H "Content-Type: application/json" -d '{"provider":"deepseek","kind":"snapshot"}'

# 扫页面上所有"像开关"的元素
curl -s -X POST http://127.0.0.1:8787/api/debug/dom \
  -H "Content-Type: application/json" -d '{"provider":"deepseek","kind":"toggles"}'

# 某个选择器命中的原始 HTML
curl -s -X POST http://127.0.0.1:8787/api/debug/dom \
  -H "Content-Type: application/json" \
  -d '{"provider":"doubao","kind":"sel","selector":"[data-testid*='"'"'send'"'"']"}'

# 点一下页面上含某文字的元素，看是否弹出菜单
curl -s -X POST http://127.0.0.1:8787/api/debug/click \
  -H "Content-Type: application/json" -d '{"provider":"doubao","word":"豆包 快速"}'

# 页面上有没有浮层挡路、里面有哪些可点的东西
curl -s -X POST http://127.0.0.1:8787/api/debug/dom \
  -H "Content-Type: application/json" -d '{"provider":"deepseek","kind":"popups"}'

# 手动清一次浮层，并回报清理后还剩什么
curl -s -X POST http://127.0.0.1:8787/api/debug/dismiss-popup \
  -H "Content-Type: application/json" -d '{"provider":"deepseek"}'
```

截图在 `~/.token-saver/data/shots/`（出错时会自动截）。

不想起服务时也可以直接跑脚本（`<venv>` 指 `~/.token-saver/venv`，
Windows 下是 `~/.token-saver/venv/Scripts/python.exe`）：

```bash
<venv>/bin/python tools/probe_popup.py deepseek       # 勘查某家站点的浮层
<venv>/bin/python tools/selftest_popup.py             # 验证清理逻辑本身没坏
```

> 运行数据目录可以用 `TOKEN_SAVER_HOME` 改到别处；想确认当前到底是哪一个：
> `curl -s http://127.0.0.1:8787/api/status`，返回里的 `stop_file` 就是它。

## 症状 → 原因 → 处理

### 服务起不来 / 连不上

| 检查 | 处理 |
| --- | --- |
| 端口被占 | `netstat -ano \| findstr 8787`，或改 `config.yaml` 的 `port` |
| 依赖没装 | 重跑 `scripts/install.py` |
| 启动报错 | 前台跑 `<venv>/bin/python server.py`（能看到报错），别用后台启动器 |
| 提示"拒绝该 Host" | 你是用域名/外网 IP 访问的。只接受 `127.0.0.1` / `localhost`；确有需要设 `server.strict_host: false` |

### 停不下来 / 关掉浏览器又自己开了

**批量任务想中止时别靠关窗口** —— 浏览器崩了能自愈是特性，反面就是
关一次它重开一次。用停机开关：

```bash
curl -s -X POST http://127.0.0.1:8787/api/stop      # 停机
curl -s -X POST http://127.0.0.1:8787/api/resume    # 恢复
curl -s http://127.0.0.1:8787/api/status            # 查询
```

MCP 里是 `stop_offload` / `resume_offload`；控制台右上角也有「■ 叫停全部」按钮。
最土的通路也在：在 `~/.token-saver/data/` 下建一个 `STOP` 空文件，同样停机
（**删掉即恢复** —— 和调 resume 完全等价）。

停机后浏览器不会被再拉起，正在等待的回答秒级中断；后续调用立即失败
而不是等满 180 秒。另外 **60 秒内浏览器被关满 3 次会自动停机**，
兜住"我只知道关窗口"这种情况 —— 所以真把它关三次也会停，
若发现是自己误触，删掉那个 `STOP` 文件或调 resume 即可。

自己验证的话：`python tools/selftest_stop.py`（含"删哨兵即恢复"那条，应当全过）。

### 直连模式看不见窗口，怎么停？／日志说"直连失败 → 退回浏览器"

**这两件是同一件事的两种误解，答案都是：照旧 `POST /api/stop`。**

DeepSeek 走纯 HTTP 直连时**不开浏览器**，所以"关窗口"这个动作根本不存在 ——
但"没有窗口"不等于"停不下来"。停机开关管得到它：`_stream_completion` 每读到
一行 SSE 就查一次 `should_stop()`，命中就抛 `HttpStopped` 并**断开连接**。
也就是说模型正在吐字的时候就能停，不用等它答完。

真机实测（2026-10-01，问了 7 秒后叫停）：客户端拿到

```json
{"ok": false, "error": "[已停机] 用户在生成过程中叫停了本次调用",
 "hint": "要继续就删掉哨兵文件 …，或 POST /api/resume"}
```

日志是 `[http] deepseek 被叫停（生成中停机，连接已断开）`。

**如果你看到的是 `[http] 直连失败 → 退回浏览器：HttpError: [已停机] …`**，
那是 v1.0.3 之前的日志撒谎：HTTP 通道什么都没坏，也没真去开浏览器，
只是适配器的 `except Exception` 把"用户叫停"吞成了"这条路走不通"。
现在这条路径抛独立的 `HttpStopped`，适配器原样抛出、`pool` 接住后直接收手。
回归断言在 `tools/selftest_stop.py` 的 `[3b]` 节。

**想根治得比停机更彻底**：`POST /api/shutdown` 连网关一起关，
当前请求随进程死掉。MCP 场景最常见的其实是"调用方别再发下一问" ——
正在跑的那一问会自己结束，不需要额外操作。

### 每次重启都要重新扫码登录

会话凭证多半是 session cookie（DeepSeek 的 `ds_session_id` 就是
`persistent=0`），浏览器一关就被浏览器自己丢掉，跟我们的代码无关。

程序会在登录成功后、以及每次提问前把凭证存到
`~/.token-saver/data/state/<站点>.json`，下次启动**赶在导航之前**注回去，
所以只要服务端没让 token 过期就不用再登录。

如果仍然每次都要扫码：

1. 跑 `python tools/verify_session.py deepseek` 看能不能存住
2. 存了却仍失效 → 服务端 token 确实过期了（站点风控/异地/长时间未活动），
   重新登录一次即可
3. 从来没存过 → 一定是**没走过 `wait_for_login`**（比如手工在窗口里登录、
   没让程序检测到），这时补一次 `tools/verify_session.py` 就能种上

### 卡住不动，最后报 `TimeoutError`（或干脆没反应）

**先看是不是浮层压住了输入框**。进场弹窗（更新日志、功能介绍、满意度调查）
会让后续每一次点击都卡到超时，而**报错信息完全看不出是弹窗的锅** ——
它只会说某个元素点不动。

```bash
curl -s -X POST http://127.0.0.1:8787/api/debug/dom \
  -H "Content-Type: application/json" -d '{"provider":"deepseek","kind":"popups"}'
```

程序本来就会自动清浮层（Esc → 站点选择器 → 通用启发式 → 点遮罩），
成功的记录会挂在返回的 `via` 字段末尾，形如 `[清弹窗:round1:Esc]`。
看到这个字段说明清理跑过但后续另有故障；没看到且列表非空则是清理失败。

清理失败时的处理：看 `kind:"popups"` 返回的容器 class，补进配置 ——

```yaml
providers:
  deepseek:
    selectors:
      popup_close:
        - "div[class*='你们看到的容器class'] [class*='close' i]"
```

**注意别补得太激进**：只写"弹窗容器里的关闭键"，不要写全局的 `[class*='close']`，
否则会把页面上正常的按钮点掉。

### 返回 `"error": "XX 未登录"`

**最常见**。去控制台点该站点的「登录」，扫码登录一次即可，之后长期有效。
建议至少登录两家，这样一家抽风能自动切换。

### 返回"抓取到空答案"

页面结构变了，或页面还没渲染完。依次试：

1. `kind:"answers"` 看有没有答案容器
2. 有容器但抓不到 → 用 `kind:"sel"` 看容器 HTML，把正确的选择器
   补进 `config.yaml` 的 `providers.<站点>.selectors.answer`
3. 没有容器 → 可能页面没加载完，加大 `runtime.ask_timeout`

### 返回"助手回复未出现"（而且等了很久）

抓到的内容是"不是答案的东西"（思考过程 / 状态行 / 空）。
用 `kind:"snapshot"` 看抓到什么：

| 抓到的是 | 原因 | 处理 |
| --- | --- | --- |
| 思考过程（"我们需要回答用户…"） | 答案选择器抓错容器 | 补精确选择器，参考 `providers.md` |
| 搜索状态行（"已完成思考，参考 N 篇资料"） | 正文还没出 | 加大该站点 `ask_timeout` |
| 空 | 页面没内容（跳回首页了） | 检查该站点是否答完会跳走 |

### 卡很久最后超时

先看是不是"页面早就答完了"。常见原因是**停止按钮残留**导致稳定计时
被不断重置。已内置 90 秒上限，若仍超时说明该站点更特殊，
调小该站点的 `ask_timeout` 让失败更快，或补精确的 `stop_button` 选择器。

### 答案答非所问（串话题）

网页端页面看着有上文，模型却答了别的内容（实测豆包把"永城"答成"睢县"）。
这是**网页端自己的会话绑定问题**。程序内置两道自愈：

- 反问缺失信息（"你说的'那边'是哪里？"）
- 跑题（提问用了"那边"等指代词，答案里却没有上文的特征词）

命中后会自动**带历史重试一次**。若仍频繁出现，把
`runtime.reuse_page_session` 设为 `false`（每轮都回填历史，牺牲 token 换稳定）。

### 新开的窗口却带着很久以前的对话 / 答案就是我自己发出去的那句话

这两个症状同源，都在**回填**这一步（2026-10-01 豆包生图轮实测）：

- **带着旧对话**：没传 `thread` 的调用会自动挂到该站点的隐式话题
  `auto-<站点>` 上，而这个键是**按站点共享**的，会把不同时间、毫不相干的任务
  全攒在一起（实测 `auto-doubao` 里既有 18 小时前的"列举中国节日"，又有之后的
  翻译和生图）。以前网页会话一断开就把整段历史回填进提示词 —— 表现就是
  "新开一个生图窗口，发出去的提示词里带着很久以前的测试对话"。
  现在隐式话题**不记也不回填**历史，会话没接上直接干净开局。
  想要"会话丢了也能接上"，**显式传 `thread`**。
- **答案是我自己发的话**：回填块当初每轮写的是"用户问：…"，而
  `core/adapter.py` 的 `_REASONING_MARKERS` 里也含"用户问"（那张表本是给
  深度思考的"复述对话"用的豁免名单）。两处文案一撞，`looks_like_echo` 的豁免
  分支就先 `return False` —— 回显判定被自己发出去的历史块关掉了。生图轮助手
  只出图、文字回答迟迟不来时，抓到的"答案"就是用户消息本身（3618 字），还被
  写进话题历史、下一轮又回填下去（6173 字）。
  现已把"铁证"（逐字相同 / 以原提问开头）排在豁免**之前**，回填块文案也换了词。

两条都有断言锁着：`tools/selftest_thread.py` + `tools/selftest_answer_pick.py`。
把回填块改回"用户问：…"这类措辞会立刻测红。

### 消息卡在输入框里，没发出去

发送候选选择器命中了**用户消息气泡**（不是发送按钮）。
程序已改为"只点真正的 `<button>` + 点完校验输入框是否清空 + 失败退回按 Enter"。
若仍出现，用 `kind:"sel"` 找到真正的发送按钮，补进
`providers.<站点>.selectors.send_button`。

### `running` 说窗口还在，其实已经关了

`/api/providers` 和 `/api/status` 里的 `running` 读的是 `BrowserManager._ctx` 这张
**本地缓存句柄表**（`running()` 就是 `list(self._ctx.keys())`），**不做存活探测**。
用户手动关掉窗口后它照样报 `true`，要等下一次提问才会在
`[alive] pid=xxx 页面确实已关闭 → 判失效` 那里被发现并自动重建 —— 所以**别拿它
判断窗口是否还在**（会被骗）。

要确认真实状态，二选一：

- **看进程**：`Get-CimInstance Win32_Process -Filter "Name='msedge.exe'"`，筛
  `CommandLine` 含 `<运行数据目录>\profiles\<站点>` 的那些。归零 = 窗口确实没了。
  （顺便能证明没误伤用户自己的 Edge —— 那些走 `AppData\Local\Microsoft\Edge\User Data`。）
- **直接提一问**：看日志有没有 `[alive] … 判失效` + `[ctx] 重建浏览器上下文`。
  这两行出现 = 会话确实是丢的，那一轮会按话题类型决定回填还是干净开局。

### 窗口自己没了 —— 不是崩溃，是"没人用它了"

**这是预期行为**（v1.0.2 起）。某个站点最后一次被用到之后闲置超过
`runtime.idle_close_seconds`（默认 180 秒），网关会主动把它的窗口收掉，
日志里会有一行：

```
[idle] pid=doubao 已闲置 184s → 自动关闭窗口
```

判断依据只有一条：**"没在用它"**。所以

- **还要接着追问** → 不会关。提问（不管是复用网页会话还是重建）都会把计时刷新，
  实测 `idle_for` 从 `51.2` 秒被追回 `0.3` 秒；
- **想让它留着** → 把 `config.yaml` 里 `runtime.idle_close_seconds` 调大，
  或直接设 `0` 关掉这个功能；
- **想立刻收干净** → `POST /api/close`（MCP `close_windows`、控制台「⧉ 关闭窗口」）。

**关窗不影响登录态**（cookie 在持久化 profile 里），下次调用自动重开，
日志是 `[ctx] … 首次启动浏览器` + `[session] 已回填 XX 个 cookie`，只多花几秒。

> 顺带一条容易误判的：`/api/status` 里 `auto_closed` 记着"最近自动关过谁"，
> `idle_for` 记着"现在谁闲了多久"。看到 `running` 少了人先看这两个字段，
> 别当成窗口崩了。**注意别和「叫停全部」搞混** —— 那个是停机，会拦下后续调用；
> 这个只收窗口，调用照常。

### 返回 `TargetClosedError`

浏览器窗口被关了（可能是手动关的、也可能被杀）。程序会自动重建并重试两次。
**直接重试这一问即可**，上下文会自动接上。

### 模型开关没自动打开

`kind:"toggles"` 扫一下该站点现在有哪些开关、文案是什么，
把结果写进 `config.yaml`：

```yaml
providers:
  <站点>:
    # 真开关（点一下切 on/off）
    toggles:
      深度思考: true
    # 假开关（得点开下拉再选菜单项）
    picks:
      "[data-testid='xxx']": K3
```

注意 `picks` 的 key 支持 CSS 选择器（含 `[` `=` 或以 `.` `#` 开头），
用站点自带的 `data-*` 属性最稳，不受文案改版影响。

### 图片抓不到

1. 确认传了 `grab_images: true`
2. 确认传了 `no_mode: true`（豆包切模型后不画图）
3. 图还在生成 → 加大 `wait_sec` 或稍后重试
4. 检查 `min_side` 是否过高（默认 512，会过滤掉小图）

### 省钱账不对

`curl -s http://127.0.0.1:8787/api/stats` 看明细。
注意**短答案外包是亏的**（盈亏平衡点 195 tokens），
`net_saved` 才是扣掉调用开销后的真实收益。

## 改了半天不生效？

**先确认配置真的生效了**：

```bash
python -c "from core import settings; print(settings.get()['runtime'])"
```

配置是三层合并（默认值 ← `<技能目录>/config.yaml` ←
`~/.token-saver/data/overrides.yaml`），
容易改错地方。另外**改完要重启服务**。

> `overrides.yaml` 是**程序自己写的**（在控制台里点站点开关就会写）。
> 手写配置请写 `config.yaml` —— 那边不会被程序覆盖，而 `overrides.yaml`
> 里已有的键会**盖住** `config.yaml`。排查"我改了怎么没生效"时，
> 两处都要看一眼。
