# agent-browser-mcp

[English](README.md) | 中文文档

一个 MCP 服务,让你的 Agent 直接操作**你正在使用的那个真实 Chrome** —— 通过 Chrome 扩展和 CDP 协议接入。Agent 工作在你已有的浏览器会话里,登录态、Cookies、已打开的标签页原本就在,不需要再开一个沙盒浏览器重新登录一遍。

它也能越过页面层:在操作系统级别发出真实的鼠标和键盘输入,应对页面内 JavaScript 不够用的场景。

## 核心能力

- **真实浏览器,真实会话** —— 接入你正在运行的 Chrome / Edge / Opera,保留登录态、Cookies 和页面上下文
- **页面读取** —— 把页面转成简化 HTML 或纯文本,长度可控,适合塞进模型上下文
- **JavaScript 执行** —— 在页面里跑任意 JS
- **原生 CDP** —— 单条命令或批量,可按标签页、扩展 id、target id 三种方式寻址
- **零标签页可用** —— 扩展管理、CDP 目标列举、标签页列举与关闭走的是扩展 service worker 通道,标签页全关也能用
- **截图** —— CDP 页面截图,以及整屏桌面截图
- **真实物理输入** —— 系统级鼠标移动/点击/拖拽、键盘输入、热键
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
| `AGENT_BROWSER_PREFERRED_BROWSER` | 未设置 | `chrome` / `edge` / `opera`。多个浏览器都连上、又没指定标签页时,默认落在哪个浏览器 |

### 命令行

```bash
agent-browser-mcp                      # 运行 MCP 服务(stdio)
agent-browser-mcp extension-path       # 打印未打包扩展的目录
agent-browser-mcp doctor               # 诊断本地环境,输出 JSON
agent-browser-mcp bridge               # 在前台运行桥
agent-browser-mcp print-hermes-config  # 打印 Hermes 配置片段
```

`doctor` 会报告扩展路径、`config.js` 是否生成、端口状态、已连接标签页数量。它还给出一个结构化判定:`cause` 是 `healthy` / `ext_never_registered` / `sw_slept_or_dropped` / `bridge_unreachable` 之一,`advice` 是对应的一句话修复建议 —— 不用再手动 `netstat` 加 `curl` 逐层刨。

## 工作原理

三层结构:

1. **Chrome 扩展**(MV3)—— 注入真实页面,通过 Chrome API 访问 `tabs`、`cookies`、`debugger`、`management`
2. **TMWebDriver 桥** —— 本地守护进程,监听 `127.0.0.1:18765`(WebSocket)和 `:18766`(HTTP)。它持有扩展连接、维护会话、转发结果。它与任何 MCP 实例解耦独立运行,缺失时由 MCP 服务自动拉起,不弹窗口。会话按 `clientId:tabId` 命名,所以多浏览器、多 profile 可以共存
3. **MCP 服务** —— 把上面这些能力暴露为 MCP 工具

到浏览器有两条通道:按标签页的会话通道,和直连扩展 service worker 的通道。后者就是为什么标签页全关时部分工具依然可用。

## 风险提示

这个服务操作的是你的真实浏览器和真实桌面。它能做的事就等于你自己能做的事,并且继承你所有已登录的会话。

- 鼠标移动、点击、输入、热键都是系统级真实输入,不是页面里的合成事件
- 页面内容属于不可信输入。Agent 读到的页面可以尝试 prompt injection,而这套工具的能力范围让后果很实际
- 这**不是**安全边界。参见 [MCP 安全最佳实践](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
- 不建议把它指向你不希望被 MCP 客户端看到的敏感账号,也不建议在共享机器或生产机器上跑

扩展申请的权限比较宽,因为这些能力确实需要:`cookies`、`tabs`、`activeTab`、`debugger`、`scripting`、`alarms`、`storage`、`declarativeNetRequest`、`management`、`bookmarks`,以及 `<all_urls>`。

## 工具列表

多数工具接受可选的 `session_id` 来指定某个标签页,不传则用当前活动标签页。标注**零标签页可用**的工具走扩展 service worker 通道,标签页全关也能用。

<details>
<summary><b>标签页与导航</b></summary>

- **get_setup_status** —— 扩展路径、端口、已连接标签页、当前会话。无参数
- **list_tabs** —— 列出已连接的标签页,每项带 `browser` 字段。无参数
- **list_all_tabs** —— *(零标签页可用)* 列出全部标签页,含 `list_tabs` 隐藏的 `chrome-extension://` 页面。这类页面永远不会成为会话,所以没有 session id,要用 `cdp_command(tab_id=...)` 操作
  - `session_id`(string,可选):问哪个浏览器
- **switch_tab** —— 切换活动标签页
  - `session_id`(string,可选)、`url_pattern`(string,可选):子串匹配、`browser`(string,可选):`chrome` / `edge` / `opera`
- **open_url** —— 当前标签页导航到 URL
  - `url`(string)、`session_id`(string,可选)、`timeout`(number,可选)
- **open_new_tab** —— 新开标签页
  - `url`(string)
- **close_tabs** —— *(零标签页可用)* 按**原生 tab id** 关闭标签页,不是 session id。对 `chrome-extension://` 页面有效,而任何走会话的路径都碰不到这类页面
  - `tab_id`(integer 或 integer 数组)、`session_id`(string,可选)
</details>

<details>
<summary><b>页面读取与执行</b></summary>

- **scan_page** —— 把页面读成简化 HTML 或纯文本
  - `session_id`(string,可选)、`text_only`(boolean,可选)、`cutlist`(boolean,可选):把重复列表裁成少量样本、`maxchars`(integer,可选)、`instruction`(string,可选)、`extra_js`(string,可选)、`timeout`(number,可选)
- **execute_js** —— 在页面里执行 JavaScript 并返回结果
  - `script`(string)、`session_id`(string,可选)、`no_monitor`(boolean,可选)、`timeout`(number,可选)
- **get_cookies** —— 读取页面 Cookies
  - `session_id`(string,可选)、`tab_id`(integer,可选)
</details>

<details>
<summary><b>CDP</b></summary>

- **cdp_command** —— 发送单条 CDP 命令
  - `method`(string):如 `Page.navigate`、`params_json`(string,可选):JSON 对象的文本形式、`session_id`(string,可选)、`tab_id`(integer,可选)、`extension_id`(string,可选)、`target_id`(string,可选)
- **cdp_batch** —— 批量发送,`batch_json` 必须是带 `cmd: "batch"` 的 JSON 对象
  - `batch_json`(string)、`session_id`(string,可选)
- **debugger_targets** —— *(零标签页可用)* 列出所有可 attach 的 CDP 目标,包括 service worker 和扩展背景页 —— 这些在 `list_tabs` 里永远看不到
  - `session_id`(string,可选)

> **关于操作*其他*扩展**:Chrome 在 attach 那一层就拒绝跨扩展调试,`tab_id`、`extension_id`、`target_id` 三种寻址方式一视同仁地被拒,除非 Chrome 带 `--silent-debugger-extension-api` 启动。这些参数的用处是操作本扩展自己的目标,以及做故障诊断。
</details>

<details>
<summary><b>扩展管理</b></summary>

- **extension_path** —— 未打包扩展的绝对路径,用于手动安装。无参数
- **list_extensions** —— *(零标签页可用)* 已安装扩展的 id、名称、启用状态、类型、版本
  - `session_id`(string,可选)
- **set_extension_enabled** —— *(零标签页可用)* 启用或禁用已安装的扩展。Chrome 没有任何 API 可以*安装*扩展,所以这里只能开关已存在的
  - `extension_id`(string)、`enabled`(boolean)、`session_id`(string,可选)
</details>

<details>
<summary><b>截图</b></summary>

- **capture_page_screenshot** —— 通过 CDP 截取页面
  - `session_id`(string,可选)、`tab_id`(integer,可选)、`format`(string,可选)、`save_path`(string,可选)、`return_base64`(boolean,可选)
- **capture_desktop_screenshot** —— 整屏截图,用于核对物理输入的效果
  - `save_path`(string,可选)
</details>

<details>
<summary><b>物理输入</b></summary>

系统级真实输入,会动你实际的鼠标指针。

- **mouse_move** —— `x`(integer)、`y`(integer)、`duration`(number,可选)
- **mouse_click** —— `x`(integer,可选)、`y`(integer,可选)、`button`(string,可选)、`clicks`(integer,可选)、`interval`(number,可选)
- **mouse_drag** —— `x1`(integer)、`y1`(integer)、`x2`(integer)、`y2`(integer)、`duration`(number,可选)、`button`(string,可选)
- **type_text** —— `text`(string)、`interval`(number,可选)、`click_x`(integer,可选)、`click_y`(integer,可选)
- **hotkey** —— `keys_csv`(string):逗号分隔,如 `ctrl,c`
- **pointer_info** —— 当前指针坐标和屏幕尺寸。无参数
</details>

## 故障排查

**客户端看到了服务,但没有连接任何标签页。** 检查扩展是否已加载,以及是否开着正常的 `http`/`https` 页面而不是空白页。然后运行 `agent-browser-mcp doctor`。

**`connected_tabs` 为 0。** 通常是扩展没加载成功、当前没有正常页面、或者扩展刚重载而页面还没刷新。刷新页面或新开一个 URL,再跑一次 `doctor`。

**客户端起不来这个服务。** 确认包装好了,以及 `agent-browser-mcp` 在 `PATH` 里 —— 如果在虚拟环境里,配置里直接填绝对路径。然后看 `doctor` 输出。

**物理输入在 macOS 上不生效。** 给终端或 MCP 客户端授予辅助功能(Accessibility)权限;需要桌面截图的话还要屏幕录制权限。

## 致谢

这里的浏览器自动化核心是从 [GenericAgent](https://github.com/lsdefine/GenericAgent) 的浏览器栈中提取出来、重新封装成 MCP 服务的。感谢该项目及其作者提供的原始实现。

以下部分来自或改编自 GenericAgent:
- `TMWebDriver.py`
- `simphtml.py`
- `tmwd_cdp_bridge` Chrome 扩展资源

如果你 fork 或二次分发,请保留这份致谢。

## 许可证

MIT
