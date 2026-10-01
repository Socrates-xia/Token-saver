# 安装与部署

把这个技能装到任何一台机器上，大约 5 分钟。

## 前置要求

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.10+ | 用系统自带的即可 |
| 浏览器 | Edge 或 Chrome | **不需要**额外下载 Playwright 内核，直接复用本机浏览器 |
| 网络 | 能访问对应站点 | 国内站点（DeepSeek/豆包/Kimi/元宝）直连；Gemini/ChatGPT 需要代理 |
| 账号 | 各站点自己的账号 | 免费注册即可，没有 API key 也能用 |

**不需要任何 API Key。** 走的是网页端界面，用的是你账号的免费额度。

## 零、装在哪里：代码与运行数据是分开的

先讲清楚这一点，后面所有路径都好理解：

| 东西 | 位置 | 说明 |
| --- | --- | --- |
| **代码** | 本技能目录 | 只读性质。升级时整目录覆盖即可 |
| **虚拟环境** | `~/.token-saver/venv/` | `install.py` 创建；不在技能目录里，所以技能包永远是"干净"的 |
| **运行数据** | `~/.token-saver/data/` | 登录态、浏览器 profile、截图、用量统计 |

`~/.token-saver` 整体可以用环境变量 `TOKEN_SAVER_HOME` 换到别处，例如
放到移动硬盘或另一块盘：

```bash
set TOKEN_SAVER_HOME=D:\token-saver-home      # Windows（当前会话）
export TOKEN_SAVER_HOME=/data/token-saver-home # macOS / Linux
```

> **为什么要把数据赶出技能目录？** 一是整包分发/上架时不该夹带自己的
> 登录 cookie；二是升级只要覆盖代码、登录态原样保留。
> 老版本把数据放在 `<技能目录>/data/`，首次运行会**自动复制过去迁移**
> （复制而不是移动，旧目录原样留着，确认没问题再自己删）。

## 一、安装依赖

### 方式 A：一键脚本（推荐）

```bash
python <skill 目录>/scripts/install.py
```

脚本会自动：检查 Python 版本 → 在 `~/.token-saver/venv` 建虚拟环境 →
装依赖 → 检查本机浏览器 → **把可直接粘贴的 MCP 配置片段打印出来**。

### 方式 B：手动

```bash
python -m venv "%USERPROFILE%\.token-saver\venv"     # Windows
python -m venv ~/.token-saver/venv                   # macOS / Linux

# Windows
"%USERPROFILE%\.token-saver\venv\Scripts\python" -m pip install -r <skill 目录>/requirements.txt
# macOS / Linux
~/.token-saver/venv/bin/python -m pip install -r <skill 目录>/requirements.txt
```

依赖 7 个：`fastapi` `uvicorn` `playwright` `pyyaml` `mcp` `httpx` `wasmtime`。

> **不用**跑 `playwright install`——那会下载 100+ MB 的内核。
> 本项目的设计就是复用你电脑上已经装好的 Edge / Chrome。

> `wasmtime` 只用于 **DeepSeek 纯 HTTP 直连**（跑随包分发的 `assets/sha3_wasm.wasm`
> 解 PoW）。装不上也没关系：直连会自动退回浏览器路线，功能不受影响，只是慢几秒。
> 想确认直连是否就绪：`python <skill 目录>/tools/test_http.py --status`

## 二、启动服务

```bash
cd <skill 目录>

# Windows
"%USERPROFILE%\.token-saver\venv\Scripts\python" server.py
# macOS / Linux
~/.token-saver/venv/bin/python server.py
```

启动后会自动打开控制台页面 `http://127.0.0.1:8787`。

想让它常驻后台，加 `--no-open`。

Windows 上还可以双击技能目录里的 `启动服务.bat`（前台，能看到日志）或
`后台启动.vbs`（无窗口常驻）。这两个脚本里写的是 `TOKEN_SAVER_HOME` 下的
默认 python 路径，换过 HOME 的话记得同步改一下。

## 三、登录各站点（关键一步）

**第一次用某个站点时，必须手动扫码/登录一次。**

1. 打开控制台 `http://127.0.0.1:8787`
2. 在"站点管理"里找到要用的站点，点 **登录**
3. 会弹出一个浏览器窗口，正常登录（微信扫码 / 账号密码都行）
4. 登录完把窗口留着别关（关掉也不影响，登录态已存盘）

登录态保存在 `~/.token-saver/data/profiles/<站点>/`，**下次不用再登录**。

建议至少登录 2 家，这样一家抽风时能自动切换：
**DeepSeek**（推理强、还能走纯 HTTP 直连）+ **豆包**（有生图能力）
是性价比最高的组合。

## 四、接给智能体用

### 4.1 通用方式：HTTP 接口

任何能发 HTTP 请求的东西（Python / Node / 脚本 / 别的 AI）都可以直接调：

```bash
curl -s -X POST http://127.0.0.1:8787/api/ask \
  -H "Content-Type: application/json" \
  -d '{"prompt":"把这段话翻译成英文：...","provider":"deepseek"}'
```

接口文档（自动生成，可交互测试）：`http://127.0.0.1:8787/docs`

### 4.2 MCP 方式（给支持 MCP 的智能体）

编辑 `~/.workbuddy/mcp.json`（找不到就新建）：

```json
{
  "mcpServers": {
    "token-saver": {
      "command": "<虚拟环境>/bin/python",
      "args": ["-B", "<skill 目录>/mcp_bridge.py"]
    }
  }
}
```

> `args` 里那个 `-B` 只是让解释器**别往技能目录里写 `__pycache__`**
> （技能目录要打包分发，缓存属运行期产物）。删掉它功能照常，只是会多出缓存目录。

Windows 上 `command` 写成：

```json
"command": "<虚拟环境>/Scripts/python.exe"
```

**注意**：路径用**绝对路径**，别用 `~`；反斜杠要写成 `/`（粘进 JSON 后
`\U` 之类会被当成转义序列，配置直接坏掉）。

> `scripts/install.py` 跑完会直接把这整段打印出来，路径已经是处理过的，
> 照抄最省事。

改完配置后：**WorkBuddy 需要在「连接器管理」页面点一次「信任」**，
新 MCP 才会生效（这一步很容易漏）。

### 4.3 Skill 方式（给能用 Agent Skills 的智能体）

把整个 `<skill 目录>` 放进对方的技能目录即可，例如：

- WorkBuddy：`~/.workbuddy/skills/`
- 别的 harness：`~/.agents/skills/`（Agent Skills 开放标准，格式通用）

智能体读到 `SKILL.md` 就知道什么时候调、怎么调。

> 目录里**没有** `.venv/`，也**不该有** `data/`（那两样在 `~/.token-saver/`）。
> 如果你看到它们出现在技能目录里，说明是照着老版本手抄的，可以放心删掉。

## 五、验证装好了

```bash
# 1. 服务活着吗
curl -s http://127.0.0.1:8787/api/providers

# 2. 端到端跑一次（换成已登录的站点）
curl -s -X POST http://127.0.0.1:8787/api/ask \
  -H "Content-Type: application/json" \
  -d '{"prompt":"只回复两个字：收到","provider":"deepseek","reset":true}'
```

看到 `"ok": true` 且 `answer` 是"收到"就算通了。

第一次调用会比较慢（要启动浏览器 + 加载页面），15~30 秒正常；
之后就快了。DeepSeek 走直连时只要 1~4 秒。

## 六、可选：换端口 / 调超时

编辑 `<skill 目录>/config.yaml`：

```yaml
server:
  port: 8787          # 换端口记得同步改 MCP 里的地址
  strict_host: true   # 只接受 127.0.0.1/localhost 的 Host 头（防 DNS rebinding）

runtime:
  ask_timeout: 180    # 单次最长等待（秒）
  reuse_page_session: true   # 追问时复用网页端会话（省 token）

browser:
  channel: msedge     # msedge | chrome | 留空用 playwright 自带内核
  headless: false     # 建议保持 false，很多站点有反自动化检测
```

改完重启服务生效。

> 配置是三层合并：**内置默认值 ← `<skill 目录>/config.yaml` ←
> `~/.token-saver/data/overrides.yaml`**。
> 后写的覆盖先写的，所以改默认值时代码和 `config.yaml` 都要改。
> `overrides.yaml` 是**程序自己写的**（控制台里点站点开关会落到那里），
> 手写配置请写 `config.yaml`，那边不会被覆盖。

## 七、卸载

```bash
# 删代码
rm -rf <skill 目录>
# 删运行数据（登录态、用量统计……）
rm -rf ~/.token-saver
```

两处都删掉才算干净。只删技能目录的话，下次重装会**沿用原来的登录态**，
这通常正是你想要的。
