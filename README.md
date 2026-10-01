# Token Saver

把简单任务外包给**本机网页端免费 AI**（DeepSeek / 豆包 / Kimi / 腾讯元宝），
省下主模型的 token 钱。**不需要任何 API Key**，用的是你自己账号的免费额度。

> ## ⚠️ 使用边界（请先读这一段）
>
> 本工具**仅供个人学习、研究与自用**，请勿用于商业用途、批量刷取或对外提供服务。
>
> 它的原理是自动化操作**你自己已登录的网页账号**，属于对各平台前端接口的
> 非官方逆向使用。因此：
>
> - 请**遵守各平台的服务条款（ToS）与 robots 约定**，包括使用频率、并发与用途限制
> - 请**自行承担**账号被风控、限流或封禁的风险；不要用主账号、不要在无人值守下长时间批量运行
> - 不要用它生成或传播违法违规内容，不要绕过平台的计费与付费机制
> - 各平台随时可能改版导致失效，这是预期内的，不是缺陷
>
> 参考的开源实现（`zerotoken` / `openclaw-zero-token` / `deepseek-free-api` 等）
> 也都在 README 里做了同样的声明。**如果你不认同这些边界，请不要使用本工具。**

## 30 秒上手

```bash
# 1. 安装（在 ~/.token-saver/venv 建虚拟环境 + 装依赖，约 1 分钟）
python scripts/install.py

# 2. 启动（脚本会把本机可用的 python 路径直接打印出来）
~/.token-saver/venv/bin/python server.py        # Windows: %USERPROFILE%\.token-saver\venv\Scripts\python server.py

# 3. 登录站点
#    浏览器里打开控制台 http://127.0.0.1:8787
#    在「站点管理」点「登录」，扫码登录一次（长期有效）

# 卡住了就先跑这个：验证浏览器驱动得起来吗（不需要网关在跑）
python tools/selfcheck.py
```

然后就能用了：

```bash
curl -s -X POST http://127.0.0.1:8787/api/ask \
  -H "Content-Type: application/json" \
  -d '{"prompt":"把下面这段翻译成中文：...","provider":"deepseek"}'
```

Windows 上也可以双击 `启动服务.bat`（前台）或 `后台启动.vbs`（无窗口常驻）。

## 什么时候用它才划算

省钱的原理：答案这段原本按**输出价**计费，外包后只按**输入价**读回来。
但每次调用有固定开销（约 300 tokens）。

**盈亏平衡点 = 答案 195 tokens（中文约 280 字）。**

| 答案规模 | 结论 |
| --- | --- |
| 短于 195 tokens | **亏钱还更慢**（网页端 7~15 秒）→ 自己写 |
| 长于 195 tokens | 开始省钱，越长越值 |

- ✅ 长翻译、长摘要、多维度整理、表格生成、资料综述、生成图片
- ❌ 一句话问答、改错别字、写个小函数、需要反复推理调试的任务

例外：**DeepSeek 可以走纯 HTTP 直连**（默认开），3~4 秒出结果，
短任务外包给它也不亏。

拿不准就先探一下：`POST /api/analyze {"prompt":"..."}`

## 目录结构

```
├── SKILL.md              # 智能体入口：什么时候用、怎么调
├── README.md             # 你正在看的这份
├── server.py             # HTTP 网关（+ 网页控制台）
├── mcp_bridge.py         # MCP 桥接，给支持 MCP 的智能体用
├── config.yaml           # 配置（手写文档，程序不会覆盖）
├── core/                 # 核心：浏览器管理、站点适配、会话、计费
│   ├── adapter.py        #   适配器基类（含内嵌 JS）
│   ├── adapters.py       #   适配器注册表
│   ├── adapter_*.py      #   各站点适配（deepseek / doubao / yuanbao …）
│   ├── fanout.py         #   并行外包调度
│   └── killswitch.py     #   停机开关
├── assets/               # 随包分发的静态资源（DeepSeek PoW 用的 WASM）
├── web/                  # 控制台页面
├── tools/                # 排障脚本 + 离线回归自检
├── references/
│   ├── setup.md          # 安装部署（含 MCP 配置、卸载）
│   ├── providers.md      # 各站点适配细节与已知坑
│   └── troubleshooting.md# 排障决策树
└── scripts/install.py    # 一键安装
```

> ### 这里**没有** `.venv/`，也不会长出 `data/`
>
> 虚拟环境在 `~/.token-saver/venv/`，运行数据（登录态、浏览器 profile、
> 截图、用量统计）在 `~/.token-saver/data/`。
>
> 数据刻意放在代码目录之外，所以：
> **整个技能目录可以直接复制/分发给别人**，不会夹带你的登录 cookie；
> 升级也只要覆盖代码，登录态原样保留。
>
> 想换位置设环境变量 `TOKEN_SAVER_HOME` 即可。

## 三种接法

| 方式 | 适合 | 说明 |
| --- | --- | --- |
| **HTTP** | 任何程序 / 任何语言 | `POST /api/ask`，最通用 |
| **MCP** | 支持 MCP 的智能体 | 工具：`ask_free_llm` / `fanout_ask` / `grab_images` 等 |
| **Skill** | 支持 Agent Skills 的 harness | 把本目录放进技能目录即可 |

## 几个有用的事实

- **登录态是持久的**：存在 `~/.token-saver/data/profiles/`，登录一次长期有效
- **自带浏览器复用**：直接用你机器上的 Edge/Chrome，不下载 100+MB 的内核
- **DeepSeek 可纯 HTTP 直连**（默认开）：不用开浏览器，首字延迟从 7~15 秒
  降到 1~2 秒。第一次仍需开一次浏览器把凭证（含 Bearer）收下来，
  之后就完全不需要了；失败会自动退回浏览器，所以开着很安全。
  开关：`runtime.http_first`、`providers.deepseek.http.enabled`；
  状态与自检：`python tools/test_http.py --status`（或 `--selftest`）
- **多轮追问**：同一个 `thread` 就是同一个话题，页面在就直接接着问（不重发历史），
  页面丢了自动重开并回填历史
- **并行外包**：`POST /api/fanout` 把一份活拆成互不依赖的多部分，
  同时派给多家站点；默认"谁快谁多干"
- **能生图**：`{"provider":"doubao","no_mode":true,"grab_images":true}`，
  图片会下载到 `~/.token-saver/data/images/`。
  **只返回这一轮新生成的图**——复用页面会话时，历史消息里的旧图会被排除掉
  （旧版本会把上一轮的图再下载一遍，而返回值看起来完全正常）。
  说"画 4 张不同姿势的"就会出 4 张；说"画一只"通常只出 1 张。
- **能叫停**：`POST /api/stop`（或控制台右上角「叫停全部」）。
  关浏览器窗口是**停不住**的 —— 自愈逻辑会把它重新打开。
  **DeepSeek 走直连时没有窗口，照样停得住**：流式读取每读一行就查一次停机开关，
  命中即抛 `HttpStopped` 并断开连接（模型吐到一半也能掐）。别被日志里的
  `直连失败 → 退回浏览器` 骗到 —— 那是 v1.0.3 之前把"用户叫停"记错了
- **窗口会自己收拾**：某个站点闲置超过 `runtime.idle_close_seconds`
  （默认 180 秒）就自动关掉它的窗口，**不用手动关**。还要接着追问就不会关
  （提问会复用网页会话，最省 token 的那条路线）。想立刻收干净：
  `POST /api/close`，MCP 调 `close_windows`，控制台右上角「⧉ 关闭窗口」。
  它**不停服务、不动登录态**，下次调用自动重开 —— 别和「叫停全部」搞混
- **自动开深度思考**：各站点实现不同，程序会自己适配（详见 `references/providers.md`）

## 工具速查（`tools/`）

`tools/` 里的脚本按用途分三档。**日常只用到前两档**，第三档是改代码时用的诊断工具。

### ① 日常会用到的

| 脚本 | 干什么 |
| --- | --- |
| `selfcheck.py` | 装完先跑：验证 Playwright / Edge 通道是否可用（不需要网关在跑）。不查登录状态 |
| `ask_cli.py` | **不起服务直接问一次**：`python tools/ask_cli.py deepseek "你的问题"`。未登录会打开浏览器等你扫码，扫完自动继续 |
| `verify_session.py` | 查某个站点的登录态是不是真的有效（会刷新并保存 storage state） |

### ② 离线回归自检（不联网、不开浏览器、不耗额度）

改完代码跑这一组。**每一项都会自报总数，退出码非 0 即失败。**
建议带 `-B`（如 `python -B tools/selftest_idle.py`）—— 不带会把 `__pycache__` 写进包内，
质量体检会报目录层级超限，打包前记得清干净。

```bash
python tools/test_http.py --selftest      # DeepSeek 直连：签名 / 会话持久化 / SSE 解析
python tools/selftest_thread.py           # 上下文接没接上、跑题判定
python tools/selftest_answer_pick.py      # 答案抽取：内嵌 JS 语法守门 + DOM 用例
python tools/selftest_fanout.py           # 并行外包调度
python tools/selftest_stop.py             # 停机开关（含"删哨兵即恢复"）
python tools/selftest_popup.py            # 弹窗清理（会真的开一次浏览器）
python tools/selftest_grab_images.py      # 生图抓取：只取本轮新图、不混旧图
python tools/selftest_idle.py             # 空闲自动关窗：三条安全线 + 计时续期
python tools/mcp_probe.py                 # MCP 桥：真握手 + 工具清单校验
```

### ③ 诊断工具（改适配器时才用）

`probe_*.py` 是逆向各站点 DOM 时的一次性探针 —— 列选择器、看答案容器层级、
采生成时序、抓 SSE 流。**平台改版导致选择器失效时，就是靠这些重新定位的。**
它们不在常规流程里，不保证输出格式稳定。

- `probe_dom` 类：`probe_doubao*.py` / `probe_yuanbao*.py` / `probe_kimi*.py`
- 时序/流：`probe_yuanbao_live.py`（生成期间采样）、`probe_sse.py`（DeepSeek 流）
- 其他：`probe_popup.py`（弹窗）、`probe_ds_session.py`、`set_yuanbao_mode.py`、
  `grab_doubao_image.py`、`extract_sha3_wasm.py`

> 这些脚本有些需要网关在跑（走 HTTP 调 `/api/debug/*`），有些不联网。
> 不确定就先看文件开头的 docstring。

## 遇到问题

先看 `references/troubleshooting.md`，绝大多数问题用这几条命令就能定位：

```bash
curl -s http://127.0.0.1:8787/api/providers        # 站点状态、是否登录
curl -s http://127.0.0.1:8787/api/status           # 是否停机、运行数据目录在哪
curl -s -X POST http://127.0.0.1:8787/api/debug/dom \
  -H "Content-Type: application/json" -d '{"provider":"deepseek","kind":"answers"}'
```

改过代码之后的离线回归：见上面的「工具速查 ②」。
（每一项都会自报总数，**退出码非 0 即失败** —— 可以直接串进脚本。）
