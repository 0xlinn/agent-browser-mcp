# agent-browser-mcp

[English](README.md) | 中文文档

一个 MCP 服务,让你的 Agent 直接操作**你正在使用的那个真实 Chrome** —— 通过 Chrome 扩展和 CDP 协议接入。Agent 工作在你已有的浏览器会话里,登录态、Cookies、已打开的标签页原本就在,不需要再开一个沙盒浏览器重新登录一遍。

当前版本:Python 包 **0.2.1** + Chrome unpacked 扩展 **2.1.2**。

它也能越过页面层:在操作系统级别发出真实的鼠标和键盘输入,应对页面内 JavaScript 不够用的场景。那五个工具是仅有的会碰你桌面的工具;`safe` 模式逐次批准,本机默认的 `lab` 模式复用本次会话批准,也可显式关闭询问。

## 核心能力

- **真实浏览器,真实会话** —— 接入你正在运行的 Chrome / Edge / Opera,保留登录态、Cookies 和页面上下文
- **默认后台** —— *选中*的标签页不等于*前台*标签页。`switch_tab` 只改目标、不提前台,页面工作在指定的标签页里进行,屏幕继续归你用
- **页面读取** —— 把页面转成简化 HTML 或纯文本,长度可控,适合塞进模型上下文。长链接压成 `#r1` 短引用,真实 URL 一并返回,搜索结果页既省 token 又还能点
- **JavaScript 执行** —— 在页面里跑任意 JS
- **后台页面输入** —— `page_click`、`page_type`、`page_press`、`page_drag` 在指定标签页内以*视口*坐标派发受信任的 CDP 输入事件,不移动你的光标、不改变可见标签页
- **等待与滚动** —— 等选择器、文本、URL 或 JS 条件成立;长页面滚动后重新读取。视区外被丢掉的内容 `scan_page` 会报出数量,不再静默丢弃
- **显式对话框策略** —— `alert`、`confirm`、`prompt`、`beforeunload` 各自有 `dismiss`/`accept`/`manual` 策略并如实上报;留在原地的对话框用 `handle_dialog` 处理
- **临时站点权限** —— 给某个 origin 授予 notifications、geolocation、camera 或 microphone 60–600 秒,到期自动恢复原设置
- **原生 CDP** —— 单条命令或批量,可按标签页、扩展 id、target id 三种方式寻址
- **登录态原生下载** —— 通过 Chrome 下载管理器和当前浏览器 profile 的 Cookie 下载附件,等待完成并返回已验证的本地路径
- **零标签页可用** —— 扩展管理、CDP 目标列举、标签页列举与关闭走的是扩展 service worker 通道,标签页全关也能用
- **截图** —— CDP 页面截图会作为 MCP 图片内容返回,也可同时落盘;另有整屏桌面截图用于核对物理输入。不支持图片的模型必须改用 `scan_page`、页面 API 或 OCR 读取内容
- **真实物理输入(需逐次批准)** —— 系统级鼠标移动/点击/拖拽、键盘输入、热键,每一次都要先通过一次批准
- **多浏览器共存** —— Chrome、Edge、Opera 可同时连同一个桥,会话互不覆盖

## 环境要求

- Python 3.10+
- Chrome、Edge 或 Opera
- macOS 或 Windows
- Claude Code,或任意其他 MCP 客户端

## 快速开始

### 1. 安装

```bash
pip install -e .
```

### 2. 加载 Chrome 扩展

项目自带一个未打包扩展,需要手动加载一次。

```bash
agent-browser-mcp extension-path
```

打开 `chrome://extensions`,开启**开发者模式**,点**加载已解压的扩展程序**,选上面命令打印出的目录。

如果你也用 Edge 或 Opera,在 `edge://extensions` 或 `opera://extensions` 里用同一个目录重复一遍即可,桥会自动区分。

然后打开一个正常的 `http://` 或 `https://` 页面。空白页不算 —— 内容脚本在 `about:blank` 上跑不起来,建立不了会话。

### 3. 在客户端里添加这个服务

**通用配置**,大多数客户端都适用:

```json
{
  "mcpServers": {
    "agent-browser-mcp": {
      "type": "stdio",
      "command": "agent-browser-mcp"
    }
  }
}
```

如果装在虚拟环境里,`command` 建议直接填可执行文件的绝对路径 —— 依赖 `PATH` 是客户端起不来这个服务最常见的原因。

<details>
<summary>Claude Code</summary>

```bash
claude mcp add agent-browser-mcp -- agent-browser-mcp
```

加 `--scope user` 可以让所有项目都能用。虚拟环境安装:

```bash
claude mcp add agent-browser-mcp -- /path/to/venv/bin/agent-browser-mcp
```

用 `/mcp` 确认已连接。
</details>

<details>
<summary>Claude Desktop</summary>

按 MCP 官方[安装指引](https://modelcontextprotocol.io/quickstart/user)操作,用上面的通用配置。示例文件:`examples/claude-desktop-config.json`。
</details>

<details>
<summary>Cursor</summary>

通用配置写进 `.cursor/mcp.json`(单个项目)或 `~/.cursor/mcp.json`(全局)。示例文件:`examples/cursor-mcp.json`。
</details>

<details>
<summary>VS Code</summary>

```bash
code --add-mcp '{"name":"agent-browser-mcp","command":"agent-browser-mcp"}'
```

也可以手写进 `.vscode/mcp.json` —— 注意 VS Code 用的 key 是 `servers`,不是 `mcpServers`。
</details>

<details>
<summary>Hermes</summary>

加到 `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agent_browser:
    command: agent-browser-mcp
    timeout: 120
    connect_timeout: 60
```

`agent-browser-mcp print-hermes-config` 可以直接打印这段。示例文件:`examples/hermes-config.yaml`。用 `hermes mcp list` 验证。
</details>

<details>
<summary>其他客户端</summary>

任何支持 stdio 的 MCP 客户端都能用。按它自己的安装指引操作,配置用上面那段通用的。
</details>

### 第一条 prompt

扩展加载好、正常页面开着之后,试试:

> 我现在开了哪些标签页?读一下当前页面并总结。

如果返回的标签页是空的,运行 `agent-browser-mcp doctor`。

## 配置

### 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `AGENT_BROWSER_TMWD_HOST` | `127.0.0.1` | 桥的绑定地址 |
| `AGENT_BROWSER_TMWD_PORT` | `18765` | WebSocket 端口。HTTP 用 `PORT+1`,`PORT+2` 是把关用的锁 socket,保证只有一个桥在托管 |
| `AGENT_BROWSER_NO_SPAWN` | 未设置 | 设为 `1` 则 MCP 服务不自动拉起桥,适合你自己手动跑桥的场景 |
| `AGENT_BROWSER_BRIDGE_AUTH` | 启用 | 仅在明确可信的本机兼容环境中设为 `off`。默认 ABM 使用持久用户 token 保护 `/link`。 |
| `AGENT_BROWSER_BRIDGE_TOKEN_FILE` | `~/.agent-browser-mcp/bridge-token` | 覆盖共享 token 文件位置。各编辑器不需要分别配置 token。 |
| `AGENT_BROWSER_BRIDGE_TOKEN` | 未设置 | 旧安装的一次性迁移来源。token 文件不存在时导入一次,此后始终以文件为准。 |
| `AGENT_BROWSER_PREFERRED_BROWSER` | 未设置 | `chrome` / `edge` / `opera`。多个浏览器都连上、又没指定标签页时,默认落在哪个浏览器 |
| `AGENT_BROWSER_MODE` | `lab` | `lab` 优先连续自动化并复用会话批准;`safe` 对每次物理输入/站点 allow 单独询问。也可用 `set_automation_profile` 只改当前 MCP 进程 |
| `AGENT_BROWSER_LAB_NO_ELICIT` | 未设置 | `lab` 下设为 `1` 可跳过物理输入与站点 allow 的 elicitation;跨进程锁和安静窗口仍生效 |
| `AGENT_BROWSER_AUTO_BEFOREUNLOAD_HOSTS` | `shell.,ttyd,code-server,jupyter,vscode-web` | `lab` 下匹配当前 host 时,普通 `open_url` 自动接受 beforeunload;显式 `intent_leave=false` 可强制保留页面 |

### 命令行

```bash
agent-browser-mcp                      # 运行 MCP 服务(stdio)
agent-browser-mcp extension-path       # 打印未打包扩展的目录
agent-browser-mcp doctor               # 诊断本地环境,输出 JSON
agent-browser-mcp bridge               # 在前台运行桥
agent-browser-mcp print-hermes-config  # 打印 Hermes 配置片段
```

`doctor` 会报告扩展路径、`config.js` 是否生成、端口状态、已连接标签页数量。它还给出一个结构化判定:`cause` 是 `healthy` / `ext_never_registered` / `sw_slept_or_dropped` / `bridge_unreachable` 之一,`advice` 是对应的一句话修复建议 —— 不用再手动 `netstat` 加 `curl` 逐层刨。

ABM 首次使用时创建 `~/.agent-browser-mcp/bridge-token`,桥和所有 MCP 进程都读取
这一个文件。关闭浏览器或编辑器不会轮换 token。卸载浏览器扩展或重装 Python 包时
会有意保留该文件,因此重装后可直接继续使用。只有要彻底清除用户数据时,才应先停止
所有 ABM 桥进程,再删除整个 `~/.agent-browser-mcp` 目录;下次启动会生成新 token。

## 工作原理

三层结构:

1. **Chrome 扩展**(MV3)—— 注入真实页面,通过 Chrome API 访问 `tabs`、`cookies`、`debugger`、`management`
2. **TMWebDriver 桥** —— 本地守护进程,监听 `127.0.0.1:18765`(WebSocket)和 `:18766`(HTTP)。它持有扩展连接、维护会话、转发结果。它与任何 MCP 实例解耦独立运行,缺失时由 MCP 服务自动拉起,不弹窗口。会话按 `clientId:tabId` 命名,所以多浏览器、多 profile 可以共存
3. **MCP 服务** —— 把上面这些能力暴露为 MCP 工具

到浏览器有两条通道:按标签页的会话通道,和直连扩展 service worker 的通道。后者就是为什么标签页全关时部分工具依然可用。

## 动手前要知道的行为

**选中标签页不会把它提到前台。** `switch_tab` 默认 `activate=false`:它只改变后续调用指向的目标。在你调用 `activate_tab`、传 `switch_tab(activate=true)`、或批准一次物理输入动作之前,屏幕上不会有任何动静。页面读取、JS 和 `page_*` 输入工具都能在后台标签页上工作。

**两类坐标,两种权限。** `page_click`/`page_drag` 用的是某个标签页内的**视口**坐标,通过 CDP 派发 —— 不移动光标、不聚焦窗口,回复里带 `foreground_changed: false`。`mouse_move`/`mouse_click`/`mouse_drag` 用的是**桌面屏幕**坐标,驱动你真实的光标。两者不可互换,把视口坐标填进 `mouse_click` 会点到完全不相干的地方。

**自动化 profile。** 未设置 `AGENT_BROWSER_MODE` 时默认 `lab`:首次物理输入/站点 `allow` 获批后在当前 MCP 会话复用;`AGENT_BROWSER_LAB_NO_ELICIT=1` 可完全跳过询问。`safe` 模式仍逐次询问。两种模式都保留跨进程锁、安静窗口和 `on_screen` 检查,所以 lab 不会把迟到输入排队或在用户动键鼠时强发。

**对话框是显式的。** `execute_js(dialog_policy=...)`、`open_url(beforeunload=...)`、`handle_dialog(action=...)` 都接受 `dismiss`(默认)、`accept`、`manual`。全局默认仍是保住页面的 dismiss;只有 lab 中命中 shell/IDE host 启发式或显式 accept 才自动离开。`handle_dialog` 在 3 秒内应答或明确返回 `no_dialog`/错误;`resolve_leave_dialog` 会先做两次协议 accept,lab 允许时才以物理 Enter 作最后兜底。

**权限是租约,不是永久授权。** `set_site_permission` 只覆盖一个 origin、60–600 秒(下限 60 是 Chrome MV3 alarm 地板),记录原设置,并在到期、显式 `reset_site_permissions`、service worker 重启后恢复。`safe` 的每次 `allow` 都要批准;`lab` 复用会话批准或按配置跳过询问。浏览器 API 给不了的东西 —— clipboard、企业托管设置、OS 级权限对话框 —— 返回 `unsupported` 或 `requires_user_action`,不会被点穿。

**验证码留在你的浏览器里。** Cloudflare Turnstile 之类的控件在同一个已连接标签页里、用 `page_click` 处理,尝试次数有上限。验证码不再推进时结果就是 `challenge_stalled`,ABM 停下来让你在那个标签页里自己处理完。ABM 绝不会拿 Playwright、headless 浏览器或独立自动化 profile 当兜底 —— 它存在的意义就是你的真实、已登录会话。

**改了工具要重载。** 工具 schema 和描述在客户端启动 MCP server 时读一次;升级后重启 MCP 会话或客户端,否则你调的还是旧签名。扩展改动需要在 `chrome://extensions` 手动 reload —— `chrome.runtime.reload()` 只会重启 service worker,不会从磁盘重新读文件。

### 并行任务的 tab 归属

使用前必须分类。**U(用户 tab)** 是任务首次 `list_tabs` 时已经存在的页面,默认不关闭、不导航。**A(Agent tab)** 是本任务 `open_new_tab` 创建的页面;保存它的 `session_id`、`generation`、`owner_id`,后续每步显式带该 session,并在清理路径调用 `close_tabs(..., owner_id=...)`。**B(借用 tab)** 是临时使用的 U;借用前记录 `original_url`,结束时若仍存在则恢复 URL,绝不关闭。

决策序:先 `list_tabs`;已有匹配页时只有只读/轻操作才借用;导航、表单或其它重状态变更优先开 A;没有匹配页则开 A;最后只关闭 A。禁止把初始快照登记为 owned、关闭 U/B、依赖共享默认 session、复用旧原生 tab id、绕过 generation 清理或泄漏 A。并行任务应各用各的 A,不要争抢同一个 U。

### 结构化状态

预期内的中断以 `status` 字段返回,而不是抛异常:

| `status` | 含义 |
|---|---|
| `ok` / `success` | 完成并在协议允许的范围内验证过 |
| `redirected` | 导航落在与请求不同的 URL(登录墙、SSO、规范化重写) |
| `navigated` | `execute_js` 脚本把页面导航走了,返回值确实丢了;`landed_url` 说明去了哪 |
| `blocked_by_dialog` | 有 JS 对话框开着,等 `handle_dialog` |
| `blocked_by_beforeunload` | 导航被取消以保住页面;要离开就带 `beforeunload="accept"` 重发 |
| `dialog_handle_failed` | 看到了对话框但应答失败;标签页可能仍被卡住 |
| `navigation_failed` / `navigation_timeout` | `open_url` 超时未完成,或浏览器报错 |
| `triggered` 且 `type="download"` | `open_url` 被浏览器下载取代。只有 CDP 同时报告 `isDownload=true` 时 `ERR_ABORTED` 才可能是正常下载语义;要完成状态和本地路径请用 `download_file` |
| `requires_user_action` | 批准被拒绝、取消或不可用 —— 什么都没做 |
| `busy` | 另一个 ABM 进程持有物理输入锁,或标签页已有挂起的 manual 执行。立即返回,绝不排队 |
| `input_activity_detected` | 批准后的安静窗口里你动了鼠标或键盘,所以没有发出物理输入 |
| `activation_failed` | 无法确认目标标签页在屏上,所以没有发出物理输入 |
| `unsupported` | 浏览器或扩展 API 提供不了(例如 clipboard 权限租约) |
| `challenge_stalled` | 浏览器验证码在尝试上限内没有进展;把标签页交还给用户 |
| `no_response` | 脚本没送达或超时 —— 有副作用的操作不要盲目重试 |
| `not_found` | 选择器没有匹配到任何元素;没有派发输入 |

## 风险提示

这个服务操作的是你的真实浏览器和真实桌面。它能做的事就等于你自己能做的事,并且继承你所有已登录的会话。

- 鼠标移动、点击、输入、热键都是系统级真实输入,不是页面里的合成事件。`safe` 逐次批准,`lab` 可复用或关闭询问;一旦允许,它驱动的是你的真实桌面
- 页面内容属于不可信输入。Agent 读到的页面可以尝试 prompt injection,而这套工具的能力范围让后果很实际
- 这**不是**安全边界。参见 [MCP 安全最佳实践](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
- 不建议把它指向你不希望被 MCP 客户端看到的敏感账号,也不建议在共享机器或生产机器上跑

扩展申请的权限比较宽,因为这些能力确实需要:`cookies`、`tabs`、`activeTab`、`debugger`、`scripting`、`alarms`、`storage`、`contentSettings`、`declarativeNetRequest`、`management`、`bookmarks`、`downloads`,以及 `<all_urls>`。

## 工具列表

多数工具接受可选的 `session_id` 来指定某个标签页,不传则用当前目标。**凡是会改状态的操作都显式传 `session_id`** —— 共享默认是单例,本桥上每个任务都能看到它,别的任务一 `switch_tab` 就会把它改掉,这正是点击落到错误页面上的原因。会话 id 形如 `chrome_a1b2c3:456`,原样传、别拆开。标注**零标签页可用**的工具走扩展 service worker 通道,标签页全关也能用。

<details>
<summary><b>标签页与导航</b></summary>

- **get_setup_status** —— 扩展路径、端口、已连接标签页、当前会话。无参数
- **get_automation_profile** —— 查看当前 MCP 进程使用 `lab` 还是 `safe` profile
- **set_automation_profile** —— 切换当前 MCP 进程的 `lab|safe` profile;覆盖值不会持久化或重载扩展
- **list_tabs** —— 列出已连接的标签页,每项带 `browser` 字段。无参数
- **list_all_tabs** —— *(零标签页可用)* 列出全部标签页,含 `list_tabs` 隐藏的 `chrome-extension://` 页面。这类页面永远不会成为会话,所以没有 session id,要用 `cdp_command(tab_id=...)` 操作
  - `session_id`(string,可选):问哪个浏览器
- **switch_tab** —— 指定后续调用的**目标**标签页。它**不会**把标签页提到前台或聚焦浏览器:`activate` 默认 `false`,改目标永远不打扰你正在看的东西。真需要标签页到前面时传 `activate=true`,或调 `activate_tab`
  - `session_id`(string,可选)、`url_pattern`(string,可选):子串匹配、`browser`(string,可选):`chrome` / `edge` / `opera`、`activate`(boolean,可选):默认 `false`
- **activate_tab** —— 把标签页提到前台并聚焦其窗口。这是显式提前台的方式,也是唯一不需要批准物理输入的方式。看返回里的 `on_screen`:Windows 上最小化的窗口不一定提得起来,`false` 意味着屏幕坐标点击会打偏
  - `session_id`(string,可选)
- **open_url** —— 当前标签页导航到 URL,并报告**实际落地**的地址。全局默认仍是 `dismiss`;lab 命中配置的 shell/IDE host 时自动 accept。协议 `navigate` 在重 SPA 失效时自动降级 `Page.navigate`。CDP 返回 `isDownload=true` 时改为返回 `{type:"download",status:"triggered"}`,不再只报 `navigation_failed`;此时附带的 `ERR_ABORTED` 是正常下载导航语义
  - `url`(string)、`session_id`(string,可选)、`timeout`(number,可选)、`beforeunload`(string,可选)、`intent_leave`(boolean,可选):`false` 强制保留页面
- **download_file** —— 通过 Chrome 原生下载管理器下载 HTTP(S) URL,使用该浏览器 profile 的 Cookie 和登录态。默认等待完成,返回 `status="completed"` 和已验证的绝对 `path`;中断返回 `failed`,超时或 `wait=false` 返回带 `download_id` 的 `in_progress`。显式 `session_id` 必须仍存活,不会静默改用其它 profile。附件应使用本工具,不要在页面里 `fetch`
  - `url`(string)、`filename`(string,可选):相对下载名称、`directory`(string,可选):任意绝对目标目录并自动建父目录;要求 `wait=true`、`wait`(boolean,可选):默认 `true`、`timeout`(number,可选):默认 60 秒,最大 1800、`session_id`(string,可选):选择浏览器 profile、`overwrite`(boolean,可选):默认 `false`,最终目标已存在时拒绝,只有显式 `true` 才替换。带 `directory` 的调用若超时会返回 `directory_applied=false`:后续搬移不再受跟踪,Chrome 可能继续下载到浏览器默认目录
- **open_new_tab** —— 新开标签页并有界等待精确 session/generation 注册,返回 `{tab_id,session_id,generation,ready,owned,opener,owner_id,load_status}`;create ACK 一旦给出 `tab_id+generation` 就登记 owned,即使 `ready=false`;`ready` 只表示 session 工具能否立即使用。保存随机 `owner_id` capability,只用于该任务收尾
  - `url`(string)、`timeout`(number,可选)、`active`(boolean,可选)、`session_id`(string,可选):选择浏览器/profile、`owner_id`(string,可选):让同一任务的多个新 tab 共用一个 owner
- **close_tabs** —— *(零标签页可用)* 接受原生数字 tab id 或完整 `client:tabId` session id;对 `chrome-extension://` 页面也有效。默认 `only_if_agent_owned=true`,必须传 `open_new_tab` 返回的 `owner_id`,并在关闭前核对当前 lifecycle generation;用户预存 tab、其它 Agent 的 tab、复用 id 的新生命周期都会拒绝。若用户已手动关掉 owned tab,收尾返回 `status=already_gone, closed_by=user`,不会拿旧原生 id 补关;真正关闭 owned tab 才返回 `closed_by=agent`;显式关闭非 owned/U 的 operator override 返回 `closed_by=none`,不会计入本任务 owned 清理。仅当用户明确要求关闭非 owned/U tab 时才设 `only_if_agent_owned=false`
  - `tab_id`(integer/string 或数组)、`session_id`(string,可选)、`owner_id`(string,安全默认下必填)、`only_if_agent_owned`(boolean,默认 `true`)
</details>

<details>
<summary><b>页面读取与执行</b></summary>

- **scan_page** —— 把页面读成简化 HTML 或纯文本。返回 `links`,把正文里每个 `#rN` 引用映射到绝对 URL;有内容留在视区外时返回 `offscreen` 和 `hint`
  - `session_id`(string,可选)、`text_only`(boolean,可选)、`cutlist`(boolean,可选):把重复列表裁成少量样本、`maxchars`(integer,可选)、`instruction`(string,可选)、`extra_js`(string,可选)、`timeout`(number,可选)
- **wait_for** —— 等条件成立后返回。别用轮询 `scan_page` 代替,那样每次都要把整个 DOM 重新序列化一遍。轮询发生在页面内,所以等 30 秒也只花一次桥往返。四个条件必须且只能给一个
  - `selector`(string,可选):CSS 匹配、`text`(string,可选):正文子串、`url_pattern`(string,可选):URL 正则、`js`(string,可选):表达式变为真值、`gone`(boolean,可选):反过来等条件不再成立、`timeout`(number,可选)、`session_id`(string,可选)
- **wait_for_url** —— 等导航落定:阻塞到标签页 URL 匹配 `url_pattern`(正则,或纯子串,两种都试),并且在 `wait_ready=false` 之外还要求 `document.readyState` 为 `complete`,然后返回最终的 `url`、`title` 和 `ready_state`。在会触发跳转的点击或 `open_url` 之后用它;`wait_for(url_pattern=...)` 只查 URL,新文档还是空白的时候就可能返回。轮询发生在页面内且跨导航分块,长等待仍然很便宜
  - `url_pattern`(string):匹配 URL 的正则或子串、`timeout`(number,可选):默认 15、`wait_ready`(boolean,可选):要求 `readyState === 'complete'`,默认 `true`、`session_id`(string,可选)
- **scroll_page** —— 滚动并报告新位置,长页面可以分几屏读完
  - `to`(string,可选):`bottom` / `top` / 像素偏移 / 要滚到可见的 CSS 选择器、`session_id`(string,可选)、`timeout`(number,可选)
- **execute_js** —— 在页面里执行 JavaScript 并返回结果。`timeout` 是覆盖对话框策略设置、monitor 快照、投递/重试、导航检查和清理的单一总 deadline;显式 `session_id` 会贯穿这些浏览器往返,不再依赖共享默认。脚本导致页面跳转时 `status` 是 `navigated` 而不是 `success`,并带 `landed_url`;这种情况下脚本返回值确实丢了,会如实报告,不会拿别的东西顶替。`dialog_policy` 决定脚本弹 `alert`/`confirm`/`prompt` 时怎么办:`dismiss`(默认)和 `accept` 直接应答并记到 `dialogs` 下,`manual` 则让原生对话框保持打开、暂停脚本并返回 `blocked_by_dialog` —— 之后调 `handle_dialog` 释放。标签页已有 manual 暂停时立即返回 `busy`
  - `script`(string)、`session_id`(string,可选)、`no_monitor`(boolean,可选)、`timeout`(number,可选)、`dialog_policy`(string,可选):`dismiss`(默认)、`accept` 或 `manual`
- **handle_dialog** —— 检查或应答某个标签页上留着的对话框。`action="manual"` 只上报不选择(`blocked_by_dialog`,没有对话框则是 `no_dialog`);`accept`/`dismiss` 应答并释放被暂停的 `execute_js` 或 `open_url`。`prompt_text` 给被 accept 的 `prompt` 提供文本
  - `action`(string):`dismiss`、`accept` 或 `manual`、`prompt_text`(string,可选)、`session_id`(string,可选)、`timeout`(number,可选):上限 3 秒
- **resolve_leave_dialog** —— 想离开 shell/ttyd/IDE 且框已弹出时使用:两次协议 accept;仅 lab 且物理输入获准时才以 Enter 兜底
  - `session_id`(string,可选)
- **upload_files** —— 给文件输入框设置文件,这是 JS 做不到的(`input.files` 只读)。整个序列走一个 CDP batch,保证 DOM nodeId 中途不失效
  - `selector`(string):`<input type=file>`、`paths`(string 或 string 数组):本地绝对路径、`session_id`(string,可选)、`timeout`(number,可选)
- **get_cookies** —— 读取页面 Cookies
  - `session_id`(string,可选)、`tab_id`(integer,可选)
- **set_cookies** —— 把 Cookie 写进真实浏览器 profile。接受单个 Cookie 对象或列表(JSON 文本也行):`name` 必填,其余可选 `value`/`url`/`domain`/`path`/`expires`(Unix 秒)/`httpOnly`/`secure`/`sameSite`。走 CDP `Network.setCookie`,所以 HttpOnly 和跨路径 Cookie 都能写;仅当 CDP 不可用时才退回 `document.cookie`,并如实报告哪些 Cookie 没能带上 HttpOnly。既没给 `url` 也没给 `domain` 的 Cookie 作用域限定在当前页面
  - `cookies`(string 或 list 或 dict)、`session_id`(string,可选)、`tab_id`(integer,可选)、`timeout`(number,可选)
- **delete_cookies** —— 按名字删除 Cookie。先走 CDP `Network.deleteCookies`,失败退回 `document.cookie` 过期法。用 `domain`/`path` 限定作用域,或给 `url` 只删一个站点
  - `name`(string)、`domain`(string,可选)、`path`(string,可选)、`url`(string,可选)、`session_id`(string,可选)、`tab_id`(integer,可选)、`timeout`(number,可选)
- **storage_get** —— 读 localStorage 或 sessionStorage。给 `key` 取单个值;不给则用 `offset`/`max_items`/`max_bytes` 分页,返回 `next_offset` 和 `truncated`;默认超时 30 秒且失败不会关闭 MCP 会话
  - `key`、`area`、`session_id`、`offset`、`max_items`、`max_bytes`、`timeout`(均可选)
- **storage_set** —— 写一个 localStorage/sessionStorage 值(非字符串值先 JSON 编码)。写完立刻回读验证,配额满或隐私模式下的失败会被如实报告,不会静默丢失
  - `key`(string)、`value`(string)、`area`(string,可选):`local`(默认)或 `session`、`session_id`(string,可选)、`timeout`(number,可选)
</details>

<details>
<summary><b>后台页面输入</b></summary>

给指定标签页派发受信任的 CDP 输入事件。它们**不会**激活标签页、聚焦窗口或移动桌面光标 —— 每次回复都带 `foreground_changed: false` 和 `input_mode: "cdp"`。所有坐标都是**视口**坐标(相对页面区域左上角),绝不是桌面坐标。

`session_id` 要显式传:调用期间驱动绑定到该标签页,结束后把共享默认还原,所以定向调用不会把别的任务的目标带走。指名一个死标签页会被拒绝,而不是偷偷换到活标签页。

- **page_click** —— 点一个 CSS 选择器或视口坐标。两种定位方式二选一:要么 `selector`,要么同时给 `x` 和 `y`。用选择器时点在元素中心,除非用 `offset_x`/`offset_y` 偏移 —— 这就是为什么跨域 iframe 里的 Cloudflare Turnstile 复选框可以点,却不需要伸进 iframe 的 DOM。选择器找不到返回 `not_found` 且不派发。页面有验证码控件时回复会带 `challenge_detected` 和 `attempts`,重复点击不再改变它时就变成 `challenge_stalled`
  - `selector`(string,可选)、`x`(number,可选)、`y`(number,可选)、`offset_x`(number,可选)、`offset_y`(number,可选)、`button`(string,可选):默认 `left`、`clicks`(integer,可选):默认 `1`、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **page_type** —— 往 CSS 选中的字段里输入文本;省略 `selector` 时输入到当前有焦点的元素。Xterm.js 容器/后代会自动改投 `.xterm-helper-textarea`;后台 tab 没有文本编辑器焦点且页面只有一个 xterm helper 时,省略 `selector` 也会自动聚焦它。目标缺失或不可输入时返回 `not_found`,不会派发文本或按键。`clear=true` 先选中已有内容;`submit_key` 事后按一个键(如 `enter`)
  - `text`(string)、`selector`(string,可选)、`clear`(boolean,可选):默认 `false`、`submit_key`(string,可选)、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **page_press** —— 在标签页里按一个键或逗号分隔的修饰键组合,如 `enter` 或 `ctrl,shift,k`
  - `keys_csv`(string)、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **page_drag** —— 在视口两点之间拖拽,作为一次不中断的事件序列
  - `x1`(number)、`y1`(number)、`x2`(number)、`y2`(number)、`duration`(number,可选):默认 `0.3`、`button`(string,可选):默认 `left`、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
</details>

<details>
<summary><b>站点权限</b></summary>

由 `chrome.contentSettings` 支撑的临时、origin 作用域权限租约。每条租约都记录原设置并在到期、显式 reset、service worker 重启或浏览器重启后恢复。

- **set_site_permission** —— 给一个 origin 设置一种权限,60–600 秒。支持:`notifications`、`geolocation`(或 `location`)、`camera`、`microphone`。`setting` 是 `allow`、`block` 或 `ask`。`safe` 下每次 `allow` 都要批准;默认 `lab` 会在当前 MCP 会话复用批准,或在 `AGENT_BROWSER_LAB_NO_ELICIT=1` 时跳过询问。拒绝返回 `requires_user_action` 且不改变任何东西。`clipboard` 接受这个名字但返回 `unsupported`,因为它的确切原状态无法恢复。省略 `origin` 用目标标签页当前 origin;只接受 `http`/`https` origin。60 秒下限是 Chrome MV3 alarm 地板,不是随便定的
  - `permission`(string)、`setting`(string):`allow`、`block` 或 `ask`、`origin`(string,可选):默认取标签页 origin、`duration_seconds`(integer,可选):60–600,默认 `300`、`session_id`(string,可选)
- **reset_site_permissions** —— 不等到期,现在就把匹配的租约恢复。`origin` 和 `permission` 都不给就恢复那个浏览器上的全部租约
  - `origin`(string,可选)、`permission`(string,可选)、`session_id`(string,可选)
</details>

<details>
<summary><b>CDP</b></summary>

- **cdp_command** —— 发送单条 CDP 命令
  - `method`(string):如 `Page.navigate`、`params_json`(string,可选):JSON 对象的文本形式、`session_id`(string,可选)、`tab_id`(integer,可选)、`extension_id`(string,可选)、`target_id`(string,可选)
- **cdp_batch** —— 批量发送,`batch_json` 必须是带 `cmd: "batch"` 的 JSON 对象
  - `batch_json`(string)、`session_id`(string,可选)
- **debugger_targets** —— *(零标签页可用)* 列出所有可 attach 的 CDP 目标,包括 service worker 和扩展背景页 —— 这些在 `list_tabs` 里永远看不到
  - `session_id`(string,可选)
- **save_pdf** —— 有界 `Page.printToPDF`,验证 PDF 后原子写文件;超时会强制释放 debugger lease
  - `save_path`(string)、`session_id`、`landscape`、`print_background`、`prefer_css_page_size`、`scale`、`page_ranges`、`timeout`(其余可选)

> **关于操作*其他*扩展**:Chrome 在 attach 那一层就拒绝跨扩展调试,`tab_id`、`extension_id`、`target_id` 三种寻址方式一视同仁地被拒,除非 Chrome 带 `--silent-debugger-extension-api` 启动。这些参数的用处是操作本扩展自己的目标,以及做故障诊断。
</details>

<details>
<summary><b>扩展管理</b></summary>

- **extension_path** —— 未打包扩展的绝对路径,用于手动安装。无参数
- **list_extensions** —— *(零标签页可用)* 已安装扩展的 id、名称、启用状态、类型、版本
  - `session_id`(string,可选)
- **set_extension_enabled** —— *(零标签页可用)* 启用或禁用已安装的扩展。Chrome 没有任何 API 可以*安装*扩展,所以这里只能开关已存在的
  - `extension_id`(string)、`enabled`(boolean)、`session_id`(string,可选)
- **uninstall_extension** —— *(零标签页可用)* 卸载另一个扩展;默认显示 Chrome 确认框,仅明确选择的测试扩展才设 `show_confirm_dialog=false`;不能通过活动通道卸载 ABM 自身
- **get_bookmarks** —— *(零标签页可用)* 读取书签树
- **create_bookmark** —— *(零标签页可用)* 创建书签或文件夹
- **remove_bookmark** —— *(零标签页可用)* 删除书签或递归删除文件夹
- **call_extension** —— *(零标签页可用)* 向另一个扩展发送 JSON;目标必须启用并通过 `externally_connectable` 允许 ABM
</details>

<details>
<summary><b>Network 与 Console 捕获</b></summary>

- **network_capture_start** —— 在指定 tab 上开始收集请求/响应和可选 body;默认 500 条环形缓冲、单 body 256 KiB
- **network_capture_stop** —— 返回当前 Network 捕获并释放 debugger lease;始终在 `finally` 语义下调用
- **console_capture_start** —— 开始收集 `console.*` 与未捕获异常
- **get_console_messages** —— 用 `offset`/`max_items` 分页读取或清空当前 console buffer
- **console_capture_stop** —— 返回剩余 console 消息并释放 debugger lease
</details>

<details>
<summary><b>截图</b></summary>

- **capture_page_screenshot** —— 通过 CDP 截取页面。返回文本元数据和 MCP 图片内容;`save_path` 只额外落盘,不会再抑制图片附件。截图已保存或已附加不等于非视觉模型看到了像素:此时必须改用 `scan_page`、`execute_js`、页面专用 API 或 OCR。默认不返回 base64,只有显式请求才返回
  - `session_id`(string,可选)、`tab_id`(integer,可选)、`format`(string,可选)、`save_path`(string,可选)、`return_base64`(boolean,可选):在结构化元数据中包含 base64,默认 `false`
- **capture_desktop_screenshot** —— 整屏截图,用于核对物理输入的效果
  - `save_path`(string,可选)
</details>

<details>
<summary><b>物理输入</b></summary>

在**桌面屏幕**坐标上的真实 OS 级输入。它移动你实际的光标,往当前有焦点的东西上输入。优先用 `page_*` 工具:它们精确、不打断你、能在后台标签页上工作。只有页面输入确实做不到时才用这些 —— 浏览器 chrome、原生文件选择器、扩展弹窗、OS 对话框。

`safe` 模式下这五个工具逐次 elicitation;默认 `lab` 在首次批准后复用当前会话授权,`AGENT_BROWSER_LAB_NO_ELICIT=1` 可跳过询问。拒绝、取消或不支持 elicitation 时返回 `requires_user_action`;无论哪种模式,锁/安静窗口/前台确认都不会跳过。

批准之后的顺序是固定的:拿跨进程锁(被占用 → **立即**返回 `busy`,绝不排队),等一段短暂安静窗口(期间你碰了鼠标或键盘 → `input_activity_detected`,什么都不发),然后把目标标签页提前台,再执行。`mouse_click` 和 `type_text` 接受 `session_id` —— 跟你给其它工具传的是同一个 —— 提的就是它;不传则回落到全局共享的默认目标,而那个可能已被别的任务改掉。用 `activate_session="none"` 直接作用在桌面上。标签页无法确认在屏上时结果是 `activation_failed` 且不发输入 —— 所以最小化的窗口产生错误,而不是点到错误的地方。

- **mouse_move** —— `x`(integer)、`y`(integer)、`duration`(number,可选)
- **mouse_click** —— `x`(integer,可选)、`y`(integer,可选)、`button`(string,可选)、`clicks`(integer,可选)、`interval`(number,可选)、`session_id`(string,可选):要提前台的标签页,正常情况就传这个、`activate_session`(string,可选):session id、`current`(默认)或 `none`
- **mouse_drag** —— `x1`(integer)、`y1`(integer)、`x2`(integer)、`y2`(integer)、`duration`(number,可选)、`button`(string,可选)
- **type_text** —— `text`(string)、`interval`(number,可选)、`click_x`(integer,可选)、`click_y`(integer,可选)、`session_id`(string,可选):要提前台的标签页,正常情况就传这个、`activate_session`(string,可选):session id、`current`(默认)或 `none`
- **hotkey** —— `keys_csv`(string):逗号分隔,如 `ctrl,c`
- **pointer_info** —— 当前指针坐标和屏幕尺寸。只读,不需要批准。无参数
</details>

## 故障排查

**客户端看到了服务,但没有连接任何标签页。** 检查扩展是否已加载,以及是否开着正常的 `http`/`https` 页面而不是空白页。然后运行 `agent-browser-mcp doctor`。

**`connected_tabs` 为 0。** 通常是扩展没加载成功、当前没有正常页面、或者扩展刚重载而页面还没刷新。刷新页面或新开一个 URL,再跑一次 `doctor`。

**客户端起不来这个服务。** 确认包装好了,以及 `agent-browser-mcp` 在 `PATH` 里 —— 如果在虚拟环境里,配置里直接填绝对路径。然后看 `doctor` 输出。

**物理输入在 macOS 上不生效。** 给终端或 MCP 客户端授予辅助功能(Accessibility)权限;需要桌面截图的话还要屏幕录制权限。

**物理输入返回 `requires_user_action` 而且从来不弹批准。** 你的 MCP 客户端没有实现 elicitation。页面级工具(`page_click`、`page_type`、`page_press`、`page_drag`)不需要批准,覆盖大多数情况;`set_site_permission(setting="allow")` 同理,没有批准提示就无法继续。

**物理输入立刻返回 `busy`。** 另一个 ABM 进程持有非排队输入租约;停止这次尝试,稍后再试,别循环。ABM 在整个动作生命周期内持续持有 OS advisory lock,即使超过元数据租约默认的 30 秒 TTL 也不会让其它进程抢占;TTL 到期绝不授权窃取仍在运行的动作。动作结束或 owner 进程退出后,下一次物理调用会自动回收 stale 元数据。不要手删锁文件、杀进程或重启桥来清锁。

**工具拒绝与文档一致的参数。** 你的客户端还拿着旧 server 的 schema:重启 MCP 会话或客户端。如果过时的是扩展,就去 `chrome://extensions` 手动 reload;`chrome.runtime.reload()` 只重启 service worker,不会从磁盘重新读文件。

**一个标签页卡住,每次调用都返回 `blocked_by_dialog` 或 `busy`。** 某个 `manual` 对话框策略留下了一个开着的原生对话框和它后面暂停的执行。在那个 `session_id` 上调 `handle_dialog(action="accept")` 或 `handle_dialog(action="dismiss")` 释放。期间其它标签页一直正常工作。

**任务结束权限还生效。** 租约在到期时恢复,但你可以用 `reset_site_permissions()` 强制恢复 —— 不带参数就恢复那个浏览器上的全部租约。如果某条租约恢复不了,它是被保留并重试而不是丢弃,所以查一下 `bridge.log`。

## 致谢

这里的浏览器自动化核心是从 [GenericAgent](https://github.com/lsdefine/GenericAgent) 的浏览器栈中提取出来、重新封装成 MCP 服务的。感谢该项目及其作者提供的原始实现。

以下部分来自或改编自 GenericAgent:
- `TMWebDriver.py`
- `simphtml.py`
- `tmwd_cdp_bridge` Chrome 扩展资源

如果你 fork 或二次分发,请保留这份致谢。

## 许可证

MIT
