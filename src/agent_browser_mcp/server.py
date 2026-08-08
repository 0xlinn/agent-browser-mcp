from __future__ import annotations

import base64
import json
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .tmwebdriver import TMWebDriver  # noqa: E402
from . import simphtml  # noqa: E402

mcp = FastMCP(
    name="agent-browser",
    instructions=(
        "Browser automation tools for the user's real Chrome/Edge session via TMWebDriver/CDP bridge. "
        "Supports page scanning, JS execution, CDP commands, screenshots, cookies, and desktop physical input. "
        "Several browsers can be connected at once; list_tabs shows a browser field per tab. "
        "Before acting, pick the target explicitly with switch_tab(browser='chrome'|'edge') or a full "
        "session_id (a 'client:tabId' string - pass it verbatim, never split it). "
        "If a result carries status='no_response', switched_session, or bridge_error: the tab slept, "
        "reconnected, or the bridge blipped - run list_tabs, switch_tab to the right target, then retry; "
        "re-run scripts with side effects only after scan_page confirms they did not land."
    ),
)

_driver: Optional[TMWebDriver] = None
_DRIVER_PORT = int(os.environ.get("AGENT_BROWSER_TMWD_PORT", "18765"))
_DRIVER_HOST = os.environ.get("AGENT_BROWSER_TMWD_HOST", "127.0.0.1")


def chrome_extension_dir() -> Path:
    return ROOT / "chrome_extension"


def ensure_config_js() -> Path:
    path = chrome_extension_dir() / "config.js"
    if not path.exists():
        path.write_text(
            f"const TID = '__ljq_{hex(random.randint(0, 99999999))[2:8]}';",
            encoding="utf-8",
        )
    return path


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def _bridge_log_path() -> Path:
    log_dir = Path.home() / ".agent-browser-mcp"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "bridge.log"
    try:
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            path.replace(path.with_suffix(".log.old"))
    except OSError:
        pass
    return path


def spawn_bridge_daemon() -> bool:
    """Start the bridge as a detached process so it outlives this MCP instance.

    Returns True once the bridge HTTP port answers. Self-hosting from an MCP
    instance is avoided because these instances are spawned per session and
    recycled, taking the bridge (and its bound ports) down with them.
    """
    # -u: unbuffered, so daemon tracebacks reach bridge.log immediately
    # instead of dying in a block buffer that never flushes.
    # Prefer pythonw.exe on Windows: it's the GUI-subsystem interpreter with no
    # console window at the binary level, so a stray console never flashes even
    # if the CREATE_NO_WINDOW flag is ignored under some launch environments.
    exe = sys.executable
    if sys.platform == "win32":
        cand = Path(exe).with_name("pythonw.exe")
        if cand.exists():
            exe = str(cand)
    cmd = [exe, "-u", "-m", "agent_browser_mcp.bridge"]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(Path.home()),
    }
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        with open(_bridge_log_path(), "ab") as log:
            kwargs["stdout"] = log
            kwargs["stderr"] = log
            subprocess.Popen(cmd, **kwargs)
    except OSError:
        return False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if _port_open(_DRIVER_HOST, _DRIVER_PORT + 1):
            return True
        time.sleep(0.25)
    return False


def get_driver() -> TMWebDriver:
    global _driver
    ensure_config_js()
    if _driver is None:
        if (
            os.environ.get("AGENT_BROWSER_NO_SPAWN") != "1"
            and not _port_open(_DRIVER_HOST, _DRIVER_PORT + 1)
        ):
            spawn_bridge_daemon()
        # If the spawn failed the constructor falls back to self-hosting,
        # which keeps the original single-process behavior working.
        _driver = TMWebDriver(host=_DRIVER_HOST, port=_DRIVER_PORT)
    return _driver


def require_driver() -> TMWebDriver:
    driver = get_driver()
    # A remote driver outlives the bridge it points at. get_driver only spawns
    # on first construction, so a daemon that dies later would leave every
    # existing MCP instance erroring forever; this check lets any tool call
    # resurrect it.
    if (
        driver.is_remote
        and os.environ.get("AGENT_BROWSER_NO_SPAWN") != "1"
        and not _port_open(_DRIVER_HOST, _DRIVER_PORT + 1)
    ):
        spawn_bridge_daemon()
    return driver


# In remote mode every session listing is an HTTP roundtrip to the bridge; a
# single tool call may want it several times (precheck, response tabs, newTab
# detection). A tiny TTL cache collapses those into one roundtrip.
_sessions_cache: Optional[tuple[float, list[dict[str, Any]]]] = None
_SESSIONS_TTL = 2.0


def invalidate_sessions_cache() -> None:
    global _sessions_cache
    _sessions_cache = None


def active_sessions(timeout: Optional[float] = None, fresh: bool = False) -> list[dict[str, Any]]:
    global _sessions_cache
    if not fresh and _sessions_cache and time.monotonic() - _sessions_cache[0] < _SESSIONS_TTL:
        return _sessions_cache[1]
    sessions = require_driver().get_all_sessions(timeout=timeout)
    _sessions_cache = (time.monotonic(), sessions)
    return sessions


def ensure_sessions() -> list[dict[str, Any]]:
    sessions = active_sessions()
    if not sessions:
        raise RuntimeError(
            "No connected browser tabs. Load the unpacked extension from the reported extension path, "
            "keep this MCP server running via Hermes, and open a normal http/https page in Chrome."
        )
    return sessions


def normalize_session_id(session_id: Optional[str]) -> Optional[str]:
    if session_id is None:
        return None
    return str(session_id)


def switch_session(
    session_id: Optional[str] = None,
    url_pattern: Optional[str] = None,
    browser: Optional[str] = None,
) -> str:
    driver = require_driver()
    if session_id is not None:
        sid = str(session_id)
        found = next((s for s in active_sessions() if str(s.get("id")) == sid), None)
        if not found:
            raise RuntimeError(f"Session {sid} not found")
        driver.default_session_id = sid
        return sid
    if browser is not None:
        # Pick a tab belonging to the named browser (chrome/edge/opera).
        # Prefer one matching url_pattern too, if given.
        want = browser.strip().lower()
        cands = [s for s in active_sessions() if str(s.get("browser", "")).lower() == want]
        if not cands:
            avail = sorted({str(s.get("browser", "?")) for s in active_sessions()})
            raise RuntimeError(f"No connected tab for browser '{want}'. Connected: {avail or 'none'}")
        if url_pattern:
            narrowed = [s for s in cands if url_pattern in str(s.get("url", ""))]
            if narrowed:
                cands = narrowed
        sid = str(cands[0]["id"])
        driver.default_session_id = sid
        return sid
    if url_pattern:
        sid = driver.set_session(url_pattern)
        if not sid:
            raise RuntimeError(f"No session matching url pattern: {url_pattern}")
        return str(sid)
    if driver.default_session_id:
        return str(driver.default_session_id)
    sessions = ensure_sessions()
    # With several browsers connected, a blind default should land on the
    # user-preferred one (AGENT_BROWSER_PREFERRED_BROWSER=chrome|edge|opera).
    pref = os.environ.get("AGENT_BROWSER_PREFERRED_BROWSER", "").strip().lower()
    if pref:
        preferred = [s for s in sessions if str(s.get("browser", "")).lower() == pref]
        if preferred:
            sessions = preferred
    driver.default_session_id = str(sessions[0]["id"])
    return str(driver.default_session_id)


def exec_js(script: str, session_id: Optional[str] = None, timeout: float = 15.0) -> dict[str, Any]:
    driver = require_driver()
    # Pass the target session through per-call instead of mutating the global
    # default_session_id. A directed call ("run this on tab Y") must not steal
    # the shared default out from under a concurrent task working on tab X.
    # session_id=None falls back to the driver's default inside execute_js.
    sid = str(session_id) if session_id is not None else None
    response = driver.execute_js(script, timeout=timeout, session_id=sid)
    if simphtml.no_response_kind(response) == "undelivered":
        # Never reached the page; retrying is side-effect-free.
        response = driver.execute_js(script, timeout=timeout, session_id=sid)
    kind = simphtml.no_response_kind(response)
    if kind:
        # Tools built on this helper (CDP, cookies, screenshots) have nothing
        # useful to return without data; fail loudly instead of returning junk.
        raise RuntimeError(
            f"Bridge no-response ({kind}): {response.get('result')}. "
            "Session may be asleep or disconnected; run list_tabs, switch_tab to a live session, then retry."
        )
    return response


def compact_tabs(timeout: Optional[float] = None, fresh: bool = False) -> list[dict[str, Any]]:
    tabs = []
    for sess in active_sessions(timeout=timeout, fresh=fresh):
        item = dict(sess)
        item.pop("connected_at", None)
        item.pop("type", None)
        tabs.append(item)
    return tabs


# Status/diagnostic tools must answer fast even when the bridge is half-dead;
# they use this short timeout and degrade instead of raising.
_STATUS_TIMEOUT = 5.0


@mcp.tool(description="Return extension path, bridge ports, and connection status for setup/diagnostics.")
def get_setup_status() -> dict[str, Any]:
    driver = get_driver()
    bridge_error = None
    try:
        sessions = compact_tabs(timeout=_STATUS_TIMEOUT, fresh=True)
    except Exception as e:
        sessions = []
        bridge_error = str(e)
    status: dict[str, Any] = {
        "extension_name": "TMWD CDP Bridge",
        "extension_path": str(chrome_extension_dir()),
        "config_js": str(ensure_config_js()),
        "tmwebdriver_host": _DRIVER_HOST,
        "tmwebdriver_ws_port": _DRIVER_PORT,
        "tmwebdriver_http_port": _DRIVER_PORT + 1,
        "remote_mode": driver.is_remote,
        "connected_tabs": len(sessions),
        "default_session_id": driver.default_session_id,
        "tabs": sessions,
        "notes": [
            "Load the unpacked extension from extension_path in chrome://extensions with Developer Mode enabled.",
            "Keep a normal http/https page open in Chrome; about:blank is not enough.",
            "The bridge runs as a detached daemon; this MCP server auto-starts it when missing.",
        ],
    }
    if bridge_error:
        status["bridge_error"] = bridge_error
    return status


@mcp.tool(description="List connected tabs across all connected browsers; each tab has a browser field (chrome/edge/opera) and a session id to pass verbatim.")
def list_tabs() -> dict[str, Any]:
    try:
        sessions = compact_tabs(timeout=_STATUS_TIMEOUT, fresh=True)
    except Exception as e:
        return {
            "default_session_id": require_driver().default_session_id,
            "tabs": [],
            "bridge_error": str(e),
        }
    return {
        "default_session_id": require_driver().default_session_id,
        "tabs": sessions,
    }


@mcp.tool(
    description=(
        "List every open tab, including chrome-extension:// pages that list_tabs hides. "
        "Those never become sessions (content scripts can't run there), so they have no "
        "session id — drive them with cdp_command(tab_id=...) instead. Works with no tabs open."
    )
)
def list_all_tabs(session_id: Optional[str] = None) -> dict[str, Any]:
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    return driver.ext_cmd({"cmd": "tabs", "all": True},
                          client_id=client_id, timeout=20.0)


@mcp.tool(
    description=(
        "Close one or more tabs by native tab id (not session id). Accepts a single id or "
        "a list. Works on chrome-extension:// pages, which no session-scoped path can reach."
    )
)
def close_tabs(tab_id: int | list[int], session_id: Optional[str] = None) -> dict[str, Any]:
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    result = driver.ext_cmd({"cmd": "tabs", "method": "close", "tabId": tab_id},
                            client_id=client_id, timeout=20.0)
    invalidate_sessions_cache()
    return {"status": "ok", "closed": tab_id, "result": result}


@mcp.tool(description="Set the active browser tab by session id, URL substring, or browser name ('chrome'/'edge'/'opera').")
def switch_tab(
    session_id: Optional[str] = None,
    url_pattern: Optional[str] = None,
    browser: Optional[str] = None,
) -> dict[str, Any]:
    sid = switch_session(session_id=session_id, url_pattern=url_pattern, browser=browser)
    return {"active_session_id": sid, "tabs": compact_tabs()}


@mcp.tool(description="Navigate the current tab to a URL using real-browser JS navigation.")
def open_url(url: str, session_id: Optional[str] = None, timeout: float = 15.0) -> dict[str, Any]:
    if session_id is not None:
        switch_session(session_id=session_id)
    driver = require_driver()
    driver.jump(url, timeout=timeout)
    invalidate_sessions_cache()
    return {
        "status": "ok",
        "active_session_id": driver.default_session_id,
        "url": url,
    }


@mcp.tool(description="Open a new browser tab with the given URL.")
def open_new_tab(url: str) -> dict[str, Any]:
    driver = require_driver()
    result = driver.newtab(url)
    invalidate_sessions_cache()
    return {"status": "ok", "result": result, "tabs": compact_tabs()}


@mcp.tool(description="Get absolute path to the unpacked Chrome extension directory for manual installation.")
def extension_path() -> dict[str, Any]:
    return {
        "extension_path": str(chrome_extension_dir()),
        "config_js": str(ensure_config_js()),
    }


@mcp.tool(description="List installed browser extensions (id, name, enabled, type, version). Works with no tabs open.")
def list_extensions(session_id: Optional[str] = None) -> dict[str, Any]:
    # Addressed to the extension itself, so this answers even with zero tabs;
    # session_id only picks which browser when several are connected.
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    return driver.ext_cmd({"cmd": "management", "method": "list"},
                          client_id=client_id, timeout=20.0)


@mcp.tool(
    description=(
        "Enable or disable an installed extension by id. Chrome exposes no API to INSTALL "
        "an extension, so this only toggles ones already present; use list_extensions for ids."
    )
)
def set_extension_enabled(extension_id: str, enabled: bool,
                          session_id: Optional[str] = None) -> dict[str, Any]:
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    result = driver.ext_cmd(
        {"cmd": "management", "method": "enable" if enabled else "disable",
         "extId": extension_id},
        client_id=client_id, timeout=20.0)
    return {"status": "ok", "extension_id": extension_id, "enabled": enabled, "result": result}


@mcp.tool(description="Read the current page as simplified HTML/text, preserving login state from the real browser.")
def scan_page(
    session_id: Optional[str] = None,
    text_only: bool = False,
    cutlist: bool = True,
    maxchars: int = 35000,
    instruction: str = "",
    extra_js: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    driver = require_driver()
    ensure_sessions()
    # Target a specific tab without permanently clobbering the shared default:
    # the monitor pipeline (get_html) does several driver.execute_js roundtrips
    # that read default_session_id, so we point it at the target for the call's
    # duration and restore it in finally. Otherwise a session_id-scoped scan_page
    # would leave the global default on this tab and hijack other tasks' work.
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    try:
        content = simphtml.get_html(
            driver,
            cutlist=cutlist,
            maxchars=maxchars,
            instruction=instruction,
            extra_js=extra_js,
            text_only=text_only,
            timeout=timeout,
        )
        active = driver.default_session_id
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    return {
        "status": "success",
        "active_session_id": active,
        "tabs": compact_tabs(),
        "content": content,
    }


@mcp.tool(description="Execute arbitrary JS in the current page context or send JSON CDP bridge commands through the page bridge.")
def execute_js(
    script: str,
    session_id: Optional[str] = None,
    no_monitor: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    driver = require_driver()
    sessions = ensure_sessions()
    before_sids = {str(s.get("id")) for s in sessions}
    # Point the shared default at the target only for this call's roundtrips
    # (execute_js_rich does baseline/diff/transient snapshots that read the
    # global default), then restore — a session_id-scoped call must not leave
    # the default parked on this tab and steal another task's session.
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    try:
        return simphtml.execute_js_rich(
            script, driver, no_monitor=no_monitor, timeout=timeout, before_sids=before_sids
        )
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default


@mcp.tool(description="Call a single Chrome DevTools Protocol command on the current or specified tab.")
def cdp_command(
    method: str,
    params_json: str = "{}",
    session_id: Optional[str] = None,
    tab_id: Optional[int] = None,
    extension_id: Optional[str] = None,
    target_id: Optional[str] = None,
) -> dict[str, Any]:
    params = json.loads(params_json or "{}")
    payload: dict[str, Any] = {"cmd": "cdp", "method": method, "params": params}
    if tab_id is not None:
        payload["tabId"] = tab_id
    if extension_id is not None or target_id is not None:
        # Non-tab debuggee. Routed via ext_cmd so it works with no tabs open.
        # NOTE both forms are refused for OTHER extensions unless Chrome runs
        # with --silent-debugger-extension-api: extensionId reports "No
        # background page with given id", targetId hits the same-extension URL
        # check. Useful for this extension's own targets and for diagnosis.
        if extension_id is not None:
            payload["extensionId"] = extension_id
        if target_id is not None:
            payload["targetId"] = target_id
        driver = require_driver()
        client_id = (str(session_id).rsplit(":", 1)[0]
                     if session_id and ":" in str(session_id) else None)
        return driver.ext_cmd(payload, client_id=client_id, timeout=20.0)
    return exec_js(json.dumps(payload), session_id=session_id, timeout=20.0)


@mcp.tool(
    description=(
        "List every CDP-attachable target, including service workers and extension "
        "background pages that list_tabs never shows. Works with no tabs open."
    )
)
def debugger_targets(session_id: Optional[str] = None) -> dict[str, Any]:
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    return driver.ext_cmd({"cmd": "debugger_targets"}, client_id=client_id, timeout=20.0)


@mcp.tool(description="Run a CDP bridge batch command; pass the full JSON command object as text.")
def cdp_batch(batch_json: str, session_id: Optional[str] = None) -> dict[str, Any]:
    payload = json.loads(batch_json)
    if payload.get("cmd") != "batch":
        raise RuntimeError("batch_json must be a JSON object with cmd='batch'")
    return exec_js(json.dumps(payload), session_id=session_id, timeout=30.0)


@mcp.tool(description="Get cookies for the current page or specified tab via the Chrome extension bridge.")
def get_cookies(session_id: Optional[str] = None, tab_id: Optional[int] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"cmd": "cookies"}
    if tab_id is not None:
        payload["tabId"] = tab_id
    return exec_js(json.dumps(payload), session_id=session_id, timeout=15.0)


@mcp.tool(description="Capture a screenshot of the current page/tab via CDP. Prefer save_path (then view the file); base64 is returned only without save_path or with return_base64=true.")
def capture_page_screenshot(
    session_id: Optional[str] = None,
    tab_id: Optional[int] = None,
    format: str = "png",
    save_path: str = "",
    return_base64: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cmd": "cdp",
        "method": "Page.captureScreenshot",
        "params": {"format": format},
    }
    if tab_id is not None:
        payload["tabId"] = tab_id
    result = exec_js(json.dumps(payload), session_id=session_id, timeout=20.0)
    data = result.get("data")
    if isinstance(data, dict) and "data" in data:
        b64 = data["data"]
    else:
        b64 = data
    out: dict[str, Any] = {"format": format}
    if save_path:
        raw = base64.b64decode(b64)
        path = Path(save_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        out["saved_to"] = str(path)
        out["size"] = len(raw)
        if return_base64:
            out["base64"] = b64
    else:
        out["base64"] = b64
    return out


def _pyautogui():
    # pyautogui reads no env vars; the failsafe (corner abort raising
    # FailSafeException mid-automation) must be disabled on the module itself.
    import pyautogui

    pyautogui.FAILSAFE = False
    return pyautogui


@mcp.tool(description="Take a desktop screenshot of the whole screen using mss; useful for physical-input verification.")
def capture_desktop_screenshot(save_path: str = "") -> dict[str, Any]:
    import mss
    from PIL import Image

    path = Path(save_path).expanduser().resolve() if save_path else (ROOT / "temp_desktop.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
        img.save(path)
    return {"saved_to": str(path), "size": path.stat().st_size}


@mcp.tool(description="Move the real mouse cursor to screen coordinates.")
def mouse_move(x: int, y: int, duration: float = 0.0) -> dict[str, Any]:
    pyautogui = _pyautogui()

    pyautogui.moveTo(x, y, duration=duration)
    return {"status": "ok", "x": x, "y": y}


@mcp.tool(description="Click on the real desktop at screen coordinates.")
def mouse_click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.1,
) -> dict[str, Any]:
    pyautogui = _pyautogui()

    if x is not None and y is not None:
        pyautogui.click(x=x, y=y, clicks=clicks, interval=interval, button=button)
    else:
        pyautogui.click(clicks=clicks, interval=interval, button=button)
    return {"status": "ok", "x": x, "y": y, "button": button, "clicks": clicks}


@mcp.tool(description="Drag the real mouse from one point to another.")
def mouse_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.3, button: str = "left") -> dict[str, Any]:
    pyautogui = _pyautogui()

    pyautogui.moveTo(x1, y1)
    pyautogui.dragTo(x2, y2, duration=duration, button=button)
    return {"status": "ok", "from": [x1, y1], "to": [x2, y2], "button": button}


@mcp.tool(description="Type text via the real keyboard, optionally after clicking a field.")
def type_text(text: str, interval: float = 0.01, click_x: Optional[int] = None, click_y: Optional[int] = None) -> dict[str, Any]:
    pyautogui = _pyautogui()

    if click_x is not None and click_y is not None:
        pyautogui.click(click_x, click_y)
        time.sleep(0.1)
    pyautogui.write(text, interval=interval)
    return {"status": "ok", "typed_chars": len(text)}


@mcp.tool(description="Send a hotkey chord like 'command,l' or 'ctrl,shift,p' via the real keyboard.")
def hotkey(keys_csv: str) -> dict[str, Any]:
    pyautogui = _pyautogui()

    keys = [k.strip() for k in keys_csv.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("keys_csv must contain at least one key")
    pyautogui.hotkey(*keys)
    return {"status": "ok", "keys": keys}


@mcp.tool(description="Report the current desktop mouse position and primary screen size.")
def pointer_info() -> dict[str, Any]:
    pyautogui = _pyautogui()

    x, y = pyautogui.position()
    w, h = pyautogui.size()
    return {"x": x, "y": y, "screen_width": w, "screen_height": h}


if __name__ == "__main__":
    ensure_config_js()
    get_driver()
    mcp.run(transport="stdio")
