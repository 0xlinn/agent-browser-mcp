<!-- mcp-name: io.github.0xlinn/agent-browser-mcp -->

# agent-browser-mcp

English | [中文文档](README.zh-CN.md)

A Model Context Protocol (MCP) server that drives **the real Chrome you are already using**, through a Chrome extension and the Chrome DevTools Protocol. Your agent works inside your existing browser session, so logins, cookies, and open tabs are all already there — no separate sandbox browser to authenticate again.

It also reaches past the page: real mouse and keyboard input at the OS level, for cases where page-level JavaScript is not enough. Those five tools are the only ones that touch your desktop: `safe` asks on every call, while the default `lab` profile reuses session approval and can explicitly disable prompts.

## Key features

- **Real browser, real session** — attaches to your running Chrome/Edge/Opera. Logged-in sites, cookies, and page context are preserved.
- **Background by default** — a *selected* tab is not a *foreground* tab. `switch_tab` retargets without raising anything, and page work runs in the tab you named while you keep using the screen.
- **Page reading** — scan any page into simplified HTML or text, sized for a model's context. Long links are shortened to `#r1` refs and the real URLs come back alongside, so a results page stays both small and navigable.
- **JavaScript execution** — run arbitrary JS in the page.
- **Background page input** — `page_click`, `page_type`, `page_press`, and `page_drag` dispatch trusted CDP input events at *viewport* coordinates inside one named tab, without moving your cursor or changing which tab is visible.
- **Waiting and scrolling** — wait for a selector, text, URL, or JS condition; scroll and re-scan long pages. `scan_page` reports how much it left outside the viewport instead of dropping it silently.
- **Explicit dialog policies** — `alert`, `confirm`, `prompt`, and `beforeunload` each get a per-call `dismiss`/`accept`/`manual` policy and are reported truthfully; `handle_dialog` resolves one that is left open.
- **Temporary site permissions** — grant notifications, geolocation, camera, or microphone to one origin for 60–600 seconds; the prior setting is restored automatically.
- **Native CDP access** — single commands or batches. Addressable by tab, extension id, or target id.
- **Tab-less operation** — extension management, CDP target listing, and tab listing/closing go straight to the extension's service worker, so they work even with zero tabs open.
- **Screenshots** — page capture via CDP is returned as MCP image content and can also be saved to disk; full desktop capture is available for physical-input checks. A model without image support must use `scan_page`, page APIs, or OCR to inspect content.
- **Real physical input, behind approval** — OS-level mouse move/click/drag, typing, and hotkeys, each requiring one accepted approval prompt for that exact call.
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
| `AGENT_BROWSER_MODE` | `lab` | `lab` prioritizes continuous automation and reuses session approvals; `safe` prompts for every physical-input/site-allow action. `set_automation_profile` changes only the current MCP process. |
| `AGENT_BROWSER_LAB_NO_ELICIT` | unset | Set to `1` to skip physical-input and site-allow elicitation in lab. The cross-process lock and quiet-input gate still apply. |
| `AGENT_BROWSER_AUTO_BEFOREUNLOAD_HOSTS` | `shell.,ttyd,code-server,jupyter,vscode-web` | In lab, ordinary `open_url` accepts beforeunload on matching current hosts. `intent_leave=false` always preserves the page. |

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

## Behaviour you should know before driving it

**Selecting a tab does not raise it.** `switch_tab` defaults to `activate=false`: it only changes which tab later calls target. Nothing moves on screen until you call `activate_tab`, pass `switch_tab(activate=true)`, or approve a physical-input action. Page reading, JS, and the `page_*` input tools all work on a background tab.

**Two kinds of coordinates, two kinds of authority.** `page_click`/`page_drag` take **viewport** coordinates inside one tab and are dispatched through CDP — no cursor movement, no window focus, `foreground_changed: false` in the reply. `mouse_move`/`mouse_click`/`mouse_drag` take **desktop screen** coordinates and drive your real cursor. The two are not interchangeable, and a viewport coordinate pasted into `mouse_click` will land somewhere else entirely.

**Automation profiles.** With `AGENT_BROWSER_MODE` unset, ABM defaults to `lab`: the first approved physical-input/site-allow action grants approval for the current MCP session, and `AGENT_BROWSER_LAB_NO_ELICIT=1` can skip elicitation. `safe` still prompts for every action. Both profiles keep the cross-process lock, quiet-input gate, and `on_screen` check, so lab never queues stale input or sends while you are using the desktop.

**Dialogs are explicit.** `execute_js(dialog_policy=...)`, `open_url(beforeunload=...)`, and `handle_dialog(action=...)` take `dismiss` (default), `accept`, or `manual`. The global default still preserves the page; only an explicit accept or lab's configured shell/IDE host heuristic leaves automatically. `handle_dialog` answers within three seconds or reports `no_dialog`/an explicit error. `resolve_leave_dialog` tries protocol accept twice and uses physical Enter only as a final, lab-approved fallback.

**Permissions are leases, not grants.** `set_site_permission` covers one origin for 60–600 seconds, records the prior setting, and restores it on expiry/reset/service-worker restart. `safe` prompts for every `allow`; `lab` reuses session approval or follows its no-elicitation setting. Browser capabilities that cannot be restored return `unsupported` or `requires_user_action`.

**Challenges stay in your browser.** A Cloudflare Turnstile or similar widget is handled in the same connected tab, by `page_click`, with a bounded number of attempts. When the challenge has not moved, the result is `challenge_stalled` and ABM stops so you can finish it yourself in that same tab. ABM never launches Playwright, a headless browser, or a separate automation profile as a fallback — the whole point is your real, logged-in session.

**Changed tools need a reload.** Tool schemas and descriptions are read once when your client starts the MCP server; after upgrading, restart the MCP session or your client, or you will keep calling the old signatures. Extension changes need a manual reload at `chrome://extensions` — `chrome.runtime.reload()` restarts the service worker without re-reading the files from disk.

### Structured statuses

Expected interruptions come back as a `status` field, not an exception:

| `status` | Meaning |
|---|---|
| `ok` / `success` | Completed and verified as far as the protocol allows. |
| `redirected` | Navigation landed on a different URL than requested (login wall, SSO, canonical rewrite). |
| `navigated` | An `execute_js` script navigated the page, so its return value is genuinely gone; `landed_url` says where it went. |
| `blocked_by_dialog` | A JavaScript dialog is open and waiting for `handle_dialog`. |
| `blocked_by_beforeunload` | Navigation was cancelled to keep the page; re-issue with `beforeunload="accept"` to leave. |
| `dialog_handle_failed` | A dialog was seen but answering it failed; the tab may still be blocked. |
| `navigation_failed` / `navigation_timeout` | `open_url` did not complete within its timeout, or the browser reported an error. |
| `requires_user_action` | Approval was declined, cancelled, or unavailable — nothing was done. |
| `busy` | Another ABM process holds the physical-input lock, or the tab already has a pending manual execution. Returned immediately, never queued. |
| `input_activity_detected` | You used the mouse or keyboard during the post-approval quiet window, so no physical input was sent. |
| `activation_failed` | The target tab could not be confirmed on screen, so no physical input was sent. |
| `unsupported` | The browser or extension API cannot provide this (e.g. clipboard permission leases). |
| `challenge_stalled` | A browser challenge made no progress within the attempt bound; hand the tab back to the user. |
| `no_response` | The script did not reach the tab or timed out — do not blindly retry anything with side effects. |
| `not_found` | The selector matched nothing; no input was dispatched. |

## Disclaimers

This server drives your real browser and your real desktop. Anything it can do, you can do — and it inherits every session you are logged into.

- Mouse moves, clicks, typing, and hotkeys are real OS-level input, not synthetic page events. `safe` prompts per call; `lab` can reuse or disable prompts. Once allowed, it drives your actual desktop.
- Page content is untrusted input. A page your agent reads can attempt prompt injection, and the tools available make that consequential.
- This is **not** a security boundary. See [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices).
- Avoid pointing it at sensitive accounts you would not want an MCP client to see, and prefer not to run it on shared or production machines.

The extension requests broad permissions because the feature set requires them: `cookies`, `tabs`, `activeTab`, `debugger`, `scripting`, `alarms`, `storage`, `contentSettings`, `declarativeNetRequest`, `management`, `bookmarks`, and `<all_urls>`.

## Tools

Most tools accept an optional `session_id` to target one specific tab; omitting it uses the current target. Pass it explicitly for anything that changes state — the shared default is a single value every task on this bridge sees, and another task retargeting it is exactly how a click lands on the wrong page. Session ids look like `chrome_a1b2c3:456`; pass them verbatim and never split them. Tools marked **no tab needed** talk to the extension's service worker and work with zero tabs open.

<details>
<summary><b>Tabs and navigation</b></summary>

- **get_setup_status** — extension path, ports, connected tabs, and the active session. No parameters.
- **get_automation_profile** / **set_automation_profile** — inspect or switch the current MCP process between `lab|safe`; switching is not persisted and does not reload the extension.
- **list_tabs** — list connected tabs. Each carries a `browser` field. No parameters.
- **list_all_tabs** — *(no tab needed)* list every open tab, including `chrome-extension://` pages that `list_tabs` hides. Those never become sessions, so they have no session id; drive them with `cdp_command(tab_id=...)`.
  - `session_id` (string, optional): which browser to ask.
- **switch_tab** — set the *target* tab for later calls. It does **not** raise the tab or focus the browser: `activate` defaults to `false`, so retargeting never disturbs what you are looking at. Pass `activate=true`, or call `activate_tab`, when you actually need the tab in front.
  - `session_id` (string, optional), `url_pattern` (string, optional): substring match, `browser` (string, optional): `chrome`, `edge`, or `opera`, `activate` (boolean, optional): default `false`.
- **activate_tab** — bring a tab to the foreground and focus its window. This is the explicit way to raise a tab, and the only one that does not involve approving physical input. Check `on_screen` in the reply: on Windows a minimised window cannot always be raised, and `false` means screen-coordinate clicks will miss.
  - `session_id` (string, optional)
- **open_url** — navigate the current tab. Global behavior remains `dismiss`; lab automatically accepts beforeunload on configured shell/IDE hosts. If the extension's `navigate` route is unavailable on a heavy SPA, ABM falls back to `Page.navigate`.
  - `url`, `session_id`, `timeout`, `beforeunload`, `intent_leave` (boolean, optional): `false` forces page preservation
- **open_new_tab** — open a tab and wait for load/session registration; returns `{tab_id,session_id,generation,ready,load_status}`. The lifecycle `generation` prevents a reused native tab id from matching an older registration, so `ready=true` is immediately usable.
  - `url`, `timeout`, `active`
- **close_tabs** — *(no tab needed)* accept native numeric tab ids or full `client:tabId` session ids, including `chrome-extension://` tabs.
</details>

<details>
<summary><b>Page reading and execution</b></summary>

- **scan_page** — read the page as simplified HTML or text. Returns `links` mapping each `#rN` ref in the content to its absolute URL, and `offscreen` + `hint` when content was left outside the viewport.
  - `session_id` (string, optional), `text_only` (boolean, optional), `cutlist` (boolean, optional): collapse repetitive lists, `maxchars` (integer, optional), `instruction` (string, optional), `extra_js` (string, optional), `timeout` (number, optional)
- **wait_for** — wait until a condition holds, then return. Use this instead of polling `scan_page`, which re-serializes the whole DOM each time. Polling happens inside the page, so a 30s wait still costs one bridge roundtrip. Exactly one condition is required.
  - `selector` (string, optional): CSS match, `text` (string, optional): substring of body text, `url_pattern` (string, optional): regex on the URL, `js` (string, optional): expression to become truthy, `gone` (boolean, optional): wait for the condition to stop holding, `timeout` (number, optional), `session_id` (string, optional)
- **wait_for_url** — wait for navigation to settle: blocks until the tab URL matches `url_pattern` (regex, or plain substring — both are tried) and, unless `wait_ready=false`, `document.readyState` is `complete`; then returns final `url`, `title` and `ready_state`. Use after a click or `open_url` that navigates; `wait_for(url_pattern=...)` only checks the URL and can return while the new document is still blank. Polls in-page across navigation chunks, so a long wait is still cheap.
  - `url_pattern` (string): regex or substring to match against the URL, `timeout` (number, optional): default 15, `wait_ready` (boolean, optional): require `readyState === 'complete'`, default `true`, `session_id` (string, optional)
- **scroll_page** — scroll and report the new position, so a long page can be read in passes.
  - `to` (string, optional): `bottom`, `top`, a pixel offset, or a CSS selector to bring into view, `session_id` (string, optional), `timeout` (number, optional)
- **execute_js** — run JavaScript in the page and return the result. `timeout` is one end-to-end deadline covering dialog-policy setup, monitor snapshots, delivery/retry, navigation inspection, and cleanup; an explicit `session_id` is forwarded through every one of those roundtrips instead of relying on the shared default. When a script navigates the page, `status` is `navigated` (not `success`) with `landed_url`; the script's return value is genuinely lost in that case and is reported as such rather than substituted. `dialog_policy` decides what happens if the script opens `alert`/`confirm`/`prompt`: `dismiss` (default) and `accept` answer it and report it under `dialogs`, while `manual` pauses the script with the native dialog still open and returns `blocked_by_dialog` — call `handle_dialog` to release it. A tab already holding a manual pause returns `busy` immediately.
  - `script` (string), `session_id` (string, optional), `no_monitor` (boolean, optional), `timeout` (number, optional), `dialog_policy` (string, optional): `dismiss` (default), `accept`, or `manual`
- **handle_dialog** — inspect or answer a dialog left open on a tab. `action="manual"` reports it without choosing (`blocked_by_dialog`, or `no_dialog` if nothing is open); `accept`/`dismiss` answer it and release any paused `execute_js` or `open_url`. `prompt_text` supplies the text for an accepted `prompt`.
  - `action`, `prompt_text`, `session_id`, `timeout` (optional, capped at three seconds)
- **resolve_leave_dialog** — for an already-open shell/ttyd/IDE leave prompt: two protocol accepts, then physical Enter only when lab permits it.
- **upload_files** — set files on a file input, which JavaScript cannot do (`input.files` is read-only). Runs as one CDP batch so the DOM node ids stay valid across the sequence.
  - `selector` (string): the `<input type=file>`, `paths` (string or array of strings): absolute local paths, `session_id` (string, optional), `timeout` (number, optional)
- **get_cookies** — read cookies for a page.
  - `session_id` (string, optional), `tab_id` (integer, optional)
- **set_cookies** — write cookies into the real browser profile. Takes one cookie object or a list (JSON text is accepted): `name` is required, plus optional `value`/`url`/`domain`/`path`/`expires` (Unix seconds)/`httpOnly`/`secure`/`sameSite`. Uses CDP `Network.setCookie`, so HttpOnly and cross-path cookies work; falls back to `document.cookie` only when CDP is unavailable, and then reports which cookies could not carry HttpOnly. Cookies with neither `url` nor `domain` are scoped to the current page.
  - `cookies` (string or list or dict), `session_id` (string, optional), `tab_id` (integer, optional), `timeout` (number, optional)
- **delete_cookies** — delete a cookie by name. Uses CDP `Network.deleteCookies`, falling back to expiring it via `document.cookie`. Scope with `domain`/`path`, or `url` to target one site.
  - `name` (string), `domain` (string, optional), `path` (string, optional), `url` (string, optional), `session_id` (string, optional), `tab_id` (integer, optional), `timeout` (number, optional)
- **storage_get** — read localStorage or sessionStorage. Omit `key` to page with `offset`/`max_items`/`max_bytes`; returns `next_offset` and `truncated`. The default timeout is 30s and a failed call does not close the MCP session.
- **storage_set** — write one localStorage/sessionStorage value (non-string values are JSON-encoded first). Verifies by read-back, so a quota-full or privacy-mode failure is reported instead of silently lost.
  - `key` (string), `value` (string), `area` (string, optional): `local` (default) or `session`, `session_id` (string, optional), `timeout` (number, optional)
</details>

<details>
<summary><b>Background page input</b></summary>

Trusted CDP input events delivered to one named tab. They do **not** activate the tab, focus its window, or move the desktop cursor — every reply carries `foreground_changed: false` and `input_mode: "cdp"`. All coordinates are **viewport** coordinates (relative to the top-left of the page area), never desktop coordinates.

Pass `session_id` explicitly: the call binds the driver to that tab for its duration and restores the shared default afterwards, so a directed call cannot leave another task's target moved. A `session_id` naming a dead tab is refused rather than redirected to a live one.

- **page_click** — click a CSS selector or viewport coordinates. Exactly one targeting mode: either `selector`, or both `x` and `y`. With a selector, the click lands at its centre unless `offset_x`/`offset_y` shift it — which is how a Cloudflare Turnstile checkbox inside a cross-origin iframe gets clicked without reaching into the iframe's DOM. A missing selector returns `not_found` and dispatches nothing. When a challenge widget is present, the reply carries `challenge_detected` and `attempts`, and becomes `challenge_stalled` once repeated clicks stop changing it.
  - `selector` (string, optional), `x` (number, optional), `y` (number, optional), `offset_x` (number, optional), `offset_y` (number, optional), `button` (string, optional): default `left`, `clicks` (integer, optional): default `1`, `session_id` (string, optional), `timeout` (number, optional): default `15`
- **page_type** — insert text into a CSS-selected field, or into whatever already has focus when `selector` is omitted. Xterm.js containers/descendants are automatically retargeted to `.xterm-helper-textarea`; when the background tab has no text editor focused and exposes one xterm helper, omitting `selector` focuses it automatically. A missing or unusable target returns `not_found` without dispatching text or key events. `clear=true` selects the existing value first; `submit_key` presses a key afterwards (e.g. `enter`).
  - `text` (string), `selector` (string, optional), `clear` (boolean, optional): default `false`, `submit_key` (string, optional), `session_id` (string, optional), `timeout` (number, optional): default `15`
- **page_press** — press a key or a comma-separated modifier chord in the tab, e.g. `enter` or `ctrl,shift,k`.
  - `keys_csv` (string), `session_id` (string, optional), `timeout` (number, optional): default `15`
- **page_drag** — drag between two viewport points as one uninterrupted event sequence.
  - `x1` (number), `y1` (number), `x2` (number), `y2` (number), `duration` (number, optional): default `0.3`, `button` (string, optional): default `left`, `session_id` (string, optional), `timeout` (number, optional): default `15`
</details>

<details>
<summary><b>Site permissions</b></summary>

Temporary, origin-scoped permission leases backed by `chrome.contentSettings`. Every lease records the prior setting and restores it — on expiry, on explicit reset, and after a service-worker restart or browser restart.

- **set_site_permission** — set one permission for one origin, for 60–600 seconds. Supported: `notifications`, `geolocation` (or `location`), `camera`, `microphone`. `setting` is `allow`, `block`, or `ask`. In `safe`, every `allow` requires approval; the default `lab` profile reuses approval for the MCP session or skips prompting when `AGENT_BROWSER_LAB_NO_ELICIT=1`. Declining returns `requires_user_action` and changes nothing. `clipboard` is accepted as a name but returns `unsupported`, because its exact prior state cannot be restored. Omit `origin` to use the target tab's current origin; only `http`/`https` origins are accepted. The 60-second minimum is Chrome's MV3 alarm floor, not an arbitrary choice.
  - `permission` (string), `setting` (string): `allow`, `block`, or `ask`, `origin` (string, optional): defaults to the tab's origin, `duration_seconds` (integer, optional): 60–600, default `300`, `session_id` (string, optional)
- **reset_site_permissions** — restore matching leases now instead of waiting for expiry. Omit both `origin` and `permission` to restore every lease on that browser.
  - `origin` (string, optional), `permission` (string, optional), `session_id` (string, optional)
</details>

<details>
<summary><b>CDP</b></summary>

- **cdp_command** — send one CDP command.
  - `method` (string): e.g. `Page.navigate`, `params_json` (string, optional): JSON object as text, `session_id` (string, optional), `tab_id` (integer, optional), `extension_id` (string, optional), `target_id` (string, optional)
- **cdp_batch** — send a batch; `batch_json` must be a JSON object with `cmd: "batch"`.
  - `batch_json` (string), `session_id` (string, optional)
- **debugger_targets** — *(no tab needed)* list every CDP-attachable target, including service workers and extension background pages that `list_tabs` never shows.
  - `session_id` (string, optional)
- **save_pdf** — bounded `Page.printToPDF`; validates PDF bytes and atomically writes `save_path`. A timeout forcibly releases its debugger lease.

> **On driving *other* extensions:** Chrome refuses cross-extension debugging at attach time, and all three addressing forms (`tab_id`, `extension_id`, `target_id`) are rejected alike unless Chrome was started with `--silent-debugger-extension-api`. These parameters are for this extension's own targets and for diagnosis.
</details>

<details>
<summary><b>Extension management</b></summary>

- **extension_path** — absolute path of the unpacked extension, for manual install. No parameters.
- **list_extensions** — *(no tab needed)* installed extensions with id, name, enabled state, type, and version.
  - `session_id` (string, optional)
- **set_extension_enabled** — *(no tab needed)* enable or disable an installed extension. Chrome exposes no API to *install* one, so this only toggles what is already there.
  - `extension_id` (string), `enabled` (boolean), `session_id` (string, optional)
- **uninstall_extension** — *(no tab needed)* uninstall another extension. Confirmation defaults on; set it off only for an explicitly selected disposable/test extension. ABM cannot uninstall itself through its active response channel.
- **get_bookmarks** / **create_bookmark** / **remove_bookmark** — *(no tab needed)* read the tree, create bookmarks/folders, and remove a bookmark or folder subtree.
- **call_extension** — *(no tab needed)* send JSON to another enabled extension; the target must allow ABM via `externally_connectable`.
</details>

<details>
<summary><b>Network and console capture</b></summary>

- **network_capture_start** / **network_capture_stop** — continuously collect bounded request/response records and optional bodies. Defaults: 500-entry ring and 256 KiB per body. Always stop in cleanup so its debugger lease is released.
- **console_capture_start** / **get_console_messages** / **console_capture_stop** — collect `console.*` and uncaught exceptions; get supports paging/clear, and stop returns the remaining messages and releases the lease.
</details>

<details>
<summary><b>Screenshots</b></summary>

- **capture_page_screenshot** — page capture via CDP. Returns text metadata plus attached MCP image content; `save_path` only adds a disk copy and never suppresses the image attachment. Saving or attaching a screenshot does not mean a non-vision model saw its pixels: use `scan_page`, `execute_js`, a page-specific API, or OCR instead. Base64 is omitted unless explicitly requested.
  - `session_id` (string, optional), `tab_id` (integer, optional), `format` (string, optional), `save_path` (string, optional), `return_base64` (boolean, optional): include base64 in structured metadata, default `false`
- **capture_desktop_screenshot** — whole-screen capture, for verifying physical input.
  - `save_path` (string, optional)
</details>

<details>
<summary><b>Physical input</b></summary>

Real OS-level input at **desktop screen** coordinates. It moves your actual cursor and types into whatever has focus. Prefer the `page_*` tools: they are precise, do not interrupt you, and work on a background tab. Reach for these only when page input genuinely cannot work — browser chrome, native file pickers, extension popups, OS dialogs.

In `safe`, each of these five asks through MCP elicitation. Default `lab` reuses approval after the first accepted action; `AGENT_BROWSER_LAB_NO_ELICIT=1` skips the prompt. Decline, cancel, or unavailable elicitation returns `requires_user_action`; every profile still enforces the lock, quiet window, and foreground check.

After approval the sequence is fixed: take the cross-process lock (contended → `busy`, returned immediately, never queued), wait out a short quiet window (you touched the mouse or keyboard → `input_activity_detected`, nothing sent), then raise the target tab, then act. `mouse_click` and `type_text` take `session_id` — the same one you pass every other tool — and raise that tab; without one they fall back to the shared global target, which another task may have changed. Use `activate_session="none"` to act on the desktop as-is. If the tab cannot be confirmed on screen the result is `activation_failed` and no input is sent, so a minimised window produces an error rather than a click into the wrong place.

- **mouse_move** — `x` (integer), `y` (integer), `duration` (number, optional)
- **mouse_click** — `x` (integer, optional), `y` (integer, optional), `button` (string, optional), `clicks` (integer, optional), `interval` (number, optional), `session_id` (string, optional): the tab to raise, and what you should normally pass, `activate_session` (string, optional): session id, `current` (default), or `none`
- **mouse_drag** — `x1` (integer), `y1` (integer), `x2` (integer), `y2` (integer), `duration` (number, optional), `button` (string, optional)
- **type_text** — `text` (string), `interval` (number, optional), `click_x` (integer, optional), `click_y` (integer, optional), `session_id` (string, optional): the tab to raise, and what you should normally pass, `activate_session` (string, optional): session id, `current` (default), or `none`
- **hotkey** — `keys_csv` (string): comma-separated, e.g. `ctrl,c`
- **pointer_info** — current cursor position and screen size. Read-only, no approval needed. No parameters.
</details>

## Troubleshooting

**The client sees the server, but no tabs are connected.** Check that the extension is loaded, and that a normal `http`/`https` page is open rather than a blank tab. Then run `agent-browser-mcp doctor`.

**`connected_tabs` is 0.** Usually the extension failed to load, there is no normal page open, or the extension was just reloaded and the page has not been refreshed. Refresh the page, or open a new URL, and run `doctor` again.

**The client cannot start the server.** Confirm the package installed, and that `agent-browser-mcp` is on `PATH` — if it is in a virtualenv, use the absolute path in your config. Then check `doctor`.

**Physical input does nothing on macOS.** Grant your terminal or MCP client Accessibility permission, plus Screen Recording if you need desktop capture.

**Physical input returns `requires_user_action` and never prompts.** Your MCP client does not implement elicitation. Page-level tools (`page_click`, `page_type`, `page_press`, `page_drag`) need no approval and cover most cases; the same applies to `set_site_permission(setting="allow")`, which cannot proceed without a prompt.

**Physical input returns `busy` right away.** Another ABM process holds the non-queued input lease. Stop this attempt and retry later rather than looping. ABM holds an OS advisory lock for the action's entire lifetime, even beyond the metadata lease's default 30-second TTL; TTL expiry never permits stealing ownership from a still-running action. After the action ends or its owner process exits, the next physical call can reclaim any stale metadata automatically. Do not delete the lock file, kill processes, or restart the bridge to clear it.

**A tool rejects arguments that match the docs.** Your client is still holding the schemas from an older server: restart the MCP session or the client. If the extension is the stale part, reload it manually at `chrome://extensions`; `chrome.runtime.reload()` restarts the service worker without re-reading files from disk.

**A tab is stuck and every call on it returns `blocked_by_dialog` or `busy`.** A `manual` dialog policy left a native dialog open and a paused execution behind it. Call `handle_dialog(action="accept")` or `handle_dialog(action="dismiss")` on that same `session_id` to release it. Other tabs keep working throughout.

**A permission is still granted after the task finished.** Leases restore on expiry, but you can force it with `reset_site_permissions()` — with no arguments it restores every lease on that browser. If a lease will not restore, it is retained and retried rather than dropped, so check `bridge.log`.

## Credits

The browser automation core here was extracted from [GenericAgent](https://github.com/lsdefine/GenericAgent)'s browser stack and repackaged as an MCP server. Thanks to that project and its author for the original implementation.

Derived from or adapted from GenericAgent:
- `TMWebDriver.py`
- `simphtml.py`
- the `tmwd_cdp_bridge` Chrome extension resources

If you fork or redistribute this, please keep the attribution.

## License

MIT
