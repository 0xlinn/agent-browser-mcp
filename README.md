<!-- mcp-name: io.github.noxenys/agent-browser-mcp -->

# agent-browser-mcp

English | [中文文档](README.zh-CN.md)

A Model Context Protocol (MCP) server that drives **the real Chrome you are already using**, through a Chrome extension and the Chrome DevTools Protocol. Your agent works inside your existing browser session, so logins, cookies, and open tabs are all already there — no separate sandbox browser to authenticate again.

It also reaches past the page: real mouse and keyboard input at the OS level, for the cases where page-level JavaScript is not enough.

## Key features

- **Real browser, real session** — attaches to your running Chrome/Edge/Opera. Logged-in sites, cookies, and page context are preserved.
- **Page reading** — scan any page into simplified HTML or text, sized for a model's context.
- **JavaScript execution** — run arbitrary JS in the page.
- **Native CDP access** — single commands or batches. Addressable by tab, extension id, or target id.
- **Tab-less operation** — extension management, CDP target listing, and tab listing/closing go straight to the extension's service worker, so they work even with zero tabs open.
- **Screenshots** — page capture via CDP, plus full desktop capture.
- **Real physical input** — OS-level mouse move/click/drag, typing, and hotkeys.
- **Multi-browser** — Chrome, Edge, and Opera can all connect to one bridge at the same time without clobbering each other's sessions.

## Requirements

- Python 3.10+
- Chrome, Edge, or Opera
- macOS or Windows
- Claude Code, or any other MCP client

## Getting started

### 1. Install

```bash
pip install -e .
```

### 2. Load the Chrome extension

This project ships an unpacked extension that has to be loaded once by hand.

```bash
agent-browser-mcp extension-path
```

Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and pick the directory that command printed.

If you also use Edge or Opera, repeat the same steps at `edge://extensions` or `opera://extensions` with the same directory. The bridge tells the browsers apart automatically.

Then open a normal `http://` or `https://` page. A blank tab is not enough — content scripts cannot run on `about:blank`, so no session is established.

### 3. Add the server to your client

**Standard config** works in most tools:

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

If you installed into a virtualenv, point `command` at the executable's absolute path instead — relying on `PATH` is the most common reason a client fails to start the server.

<details>
<summary>Claude Code</summary>

```bash
claude mcp add agent-browser-mcp -- agent-browser-mcp
```

Add `--scope user` to make it available across all projects. For a virtualenv install:

```bash
claude mcp add agent-browser-mcp -- /path/to/venv/bin/agent-browser-mcp
```

Verify with `/mcp`.
</details>

<details>
<summary>Claude Desktop</summary>

Follow the MCP install [guide](https://modelcontextprotocol.io/quickstart/user) and use the standard config above. An example file is included at `examples/claude-desktop-config.json`.
</details>

<details>
<summary>Cursor</summary>

Put the standard config in `.cursor/mcp.json` for one project, or `~/.cursor/mcp.json` globally. An example file is included at `examples/cursor-mcp.json`.
</details>

<details>
<summary>VS Code</summary>

```bash
code --add-mcp '{"name":"agent-browser-mcp","command":"agent-browser-mcp"}'
```

Or write it into `.vscode/mcp.json` by hand — note that VS Code's key is `servers`, not `mcpServers`.
</details>

<details>
<summary>Hermes</summary>

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agent_browser:
    command: agent-browser-mcp
    timeout: 120
    connect_timeout: 60
```

`agent-browser-mcp print-hermes-config` prints this snippet. An example file is included at `examples/hermes-config.yaml`. Verify with `hermes mcp list`.
</details>

<details>
<summary>Other clients</summary>

Any MCP client that speaks stdio will work. Follow its own install guide and use the standard config above.
</details>

### Your first prompt

Once the extension is loaded and a normal page is open, try:

> What tabs do I have open? Read the current page and summarise it.

If tabs come back empty, run `agent-browser-mcp doctor`.

## Configuration

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_BROWSER_TMWD_HOST` | `127.0.0.1` | Bridge bind address. |
| `AGENT_BROWSER_TMWD_PORT` | `18765` | WebSocket port. HTTP uses `PORT+1`, and `PORT+2` is a lock socket that keeps exactly one bridge hosting. |
| `AGENT_BROWSER_NO_SPAWN` | unset | Set to `1` to stop the MCP server from auto-starting the bridge. Use it when you run the bridge yourself. |
| `AGENT_BROWSER_PREFERRED_BROWSER` | unset | `chrome`, `edge`, or `opera`. Which browser wins when several are connected and no tab is specified. |

### CLI

```bash
agent-browser-mcp                      # run the MCP server (stdio)
agent-browser-mcp extension-path       # print the unpacked extension directory
agent-browser-mcp doctor               # diagnose the local setup, as JSON
agent-browser-mcp bridge               # run the bridge in the foreground
agent-browser-mcp print-hermes-config  # print a Hermes config snippet
```

`doctor` reports the extension path, whether `config.js` was generated, port state, and connected tab count. It also returns a structured verdict: `cause` is one of `healthy`, `ext_never_registered`, `sw_slept_or_dropped`, or `bridge_unreachable`, and `advice` is the matching one-line fix — no manual `netstat` and `curl` archaeology.

## How it works

Three layers:

1. **Chrome extension** (MV3) — injected into real pages, reaches `tabs`, `cookies`, `debugger`, and `management` through Chrome APIs.
2. **TMWebDriver bridge** — a local daemon on `127.0.0.1:18765` (WebSocket) and `:18766` (HTTP). It owns the extension connections, tracks sessions, and relays results. It runs detached from any MCP instance, and the MCP server starts it on demand with no console window. Sessions are keyed `clientId:tabId`, so several browsers and profiles coexist.
3. **MCP server** — exposes the whole thing as MCP tools.

Two channels reach the browser: a per-tab session channel, and a direct channel to the extension's service worker. The second one is why some tools keep working when every tab is closed.

## Disclaimers

This server drives your real browser and your real desktop. Anything it can do, you can do — and it inherits every session you are logged into.

- Mouse moves, clicks, typing, and hotkeys are real OS-level input, not synthetic page events.
- Page content is untrusted input. A page your agent reads can attempt prompt injection, and the tools available make that consequential.
- This is **not** a security boundary. See [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices).
- Avoid pointing it at sensitive accounts you would not want an MCP client to see, and prefer not to run it on shared or production machines.

The extension requests broad permissions because the feature set requires them: `cookies`, `tabs`, `activeTab`, `debugger`, `scripting`, `alarms`, `storage`, `declarativeNetRequest`, `management`, `bookmarks`, and `<all_urls>`.

## Tools

Most tools accept an optional `session_id` to target one specific tab; omitting it uses the active tab. Tools marked **no tab needed** talk to the extension's service worker and work with zero tabs open.

<details>
<summary><b>Tabs and navigation</b></summary>

- **get_setup_status** — extension path, ports, connected tabs, and the active session. No parameters.
- **list_tabs** — list connected tabs. Each carries a `browser` field. No parameters.
- **list_all_tabs** — *(no tab needed)* list every open tab, including `chrome-extension://` pages that `list_tabs` hides. Those never become sessions, so they have no session id; drive them with `cdp_command(tab_id=...)`.
  - `session_id` (string, optional): which browser to ask.
- **switch_tab** — set the active tab.
  - `session_id` (string, optional), `url_pattern` (string, optional): substring match, `browser` (string, optional): `chrome`, `edge`, or `opera`.
- **open_url** — navigate the current tab.
  - `url` (string), `session_id` (string, optional), `timeout` (number, optional)
- **open_new_tab** — open a new tab.
  - `url` (string)
- **close_tabs** — *(no tab needed)* close tabs by **native tab id**, not session id. Works on `chrome-extension://` pages, which no session-scoped path can reach.
  - `tab_id` (integer or array of integers), `session_id` (string, optional)
</details>

<details>
<summary><b>Page reading and execution</b></summary>

- **scan_page** — read the page as simplified HTML or text.
  - `session_id` (string, optional), `text_only` (boolean, optional), `cutlist` (boolean, optional): collapse repetitive lists, `maxchars` (integer, optional), `instruction` (string, optional), `extra_js` (string, optional), `timeout` (number, optional)
- **execute_js** — run JavaScript in the page and return the result.
  - `script` (string), `session_id` (string, optional), `no_monitor` (boolean, optional), `timeout` (number, optional)
- **get_cookies** — read cookies for a page.
  - `session_id` (string, optional), `tab_id` (integer, optional)
</details>

<details>
<summary><b>CDP</b></summary>

- **cdp_command** — send one CDP command.
  - `method` (string): e.g. `Page.navigate`, `params_json` (string, optional): JSON object as text, `session_id` (string, optional), `tab_id` (integer, optional), `extension_id` (string, optional), `target_id` (string, optional)
- **cdp_batch** — send a batch; `batch_json` must be a JSON object with `cmd: "batch"`.
  - `batch_json` (string), `session_id` (string, optional)
- **debugger_targets** — *(no tab needed)* list every CDP-attachable target, including service workers and extension background pages that `list_tabs` never shows.
  - `session_id` (string, optional)

> **On driving *other* extensions:** Chrome refuses cross-extension debugging at attach time, and all three addressing forms (`tab_id`, `extension_id`, `target_id`) are rejected alike unless Chrome was started with `--silent-debugger-extension-api`. These parameters are for this extension's own targets and for diagnosis.
</details>

<details>
<summary><b>Extension management</b></summary>

- **extension_path** — absolute path of the unpacked extension, for manual install. No parameters.
- **list_extensions** — *(no tab needed)* installed extensions with id, name, enabled state, type, and version.
  - `session_id` (string, optional)
- **set_extension_enabled** — *(no tab needed)* enable or disable an installed extension. Chrome exposes no API to *install* one, so this only toggles what is already there.
  - `extension_id` (string), `enabled` (boolean), `session_id` (string, optional)
</details>

<details>
<summary><b>Screenshots</b></summary>

- **capture_page_screenshot** — page capture via CDP.
  - `session_id` (string, optional), `tab_id` (integer, optional), `format` (string, optional), `save_path` (string, optional), `return_base64` (boolean, optional)
- **capture_desktop_screenshot** — whole-screen capture, for verifying physical input.
  - `save_path` (string, optional)
</details>

<details>
<summary><b>Physical input</b></summary>

Real OS-level input. It moves your actual cursor.

- **mouse_move** — `x` (integer), `y` (integer), `duration` (number, optional)
- **mouse_click** — `x` (integer, optional), `y` (integer, optional), `button` (string, optional), `clicks` (integer, optional), `interval` (number, optional)
- **mouse_drag** — `x1` (integer), `y1` (integer), `x2` (integer), `y2` (integer), `duration` (number, optional), `button` (string, optional)
- **type_text** — `text` (string), `interval` (number, optional), `click_x` (integer, optional), `click_y` (integer, optional)
- **hotkey** — `keys_csv` (string): comma-separated, e.g. `ctrl,c`
- **pointer_info** — current cursor position and screen size. No parameters.
</details>

## Troubleshooting

**The client sees the server, but no tabs are connected.** Check that the extension is loaded, and that a normal `http`/`https` page is open rather than a blank tab. Then run `agent-browser-mcp doctor`.

**`connected_tabs` is 0.** Usually the extension failed to load, there is no normal page open, or the extension was just reloaded and the page has not been refreshed. Refresh the page, or open a new URL, and run `doctor` again.

**The client cannot start the server.** Confirm the package installed, and that `agent-browser-mcp` is on `PATH` — if it is in a virtualenv, use the absolute path in your config. Then check `doctor`.

**Physical input does nothing on macOS.** Grant your terminal or MCP client Accessibility permission, plus Screen Recording if you need desktop capture.

## Credits

The browser automation core here was extracted from [GenericAgent](https://github.com/lsdefine/GenericAgent)'s browser stack and repackaged as an MCP server. Thanks to that project and its author for the original implementation.

Derived from or adapted from GenericAgent:
- `TMWebDriver.py`
- `simphtml.py`
- the `tmwd_cdp_bridge` Chrome extension resources

If you fork or redistribute this, please keep the attribution.

## License

MIT
