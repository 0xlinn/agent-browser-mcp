from __future__ import annotations

import base64
import functools
import inspect
import json
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image as MCPImage
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field, StrictBool

# All tools share one TMWebDriver whose target (default_session_id) is mutable
# state. When the MCP lowlevel server dispatches concurrent requests, two tools
# running in parallel both save/restore that global and race each other — a
# scan_page and an execute_js in the same turn can read/write different tabs
# than the ones they named. Serialize tool execution with a single RLock; the
# cost is lost parallelism, the win is that directed calls stay directed.
_TOOL_LOCK = threading.RLock()
_DRIVER_LOCK = threading.Lock()

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .tmwebdriver import TMWebDriver  # noqa: E402
from . import simphtml  # noqa: E402
from . import physical_input  # noqa: E402
from .page_input import (  # noqa: E402
    ChallengeAttemptTracker,
    InputValidationError,
    click_commands,
    drag_commands,
    press_commands,
    resolve_selector_script,
    type_commands,
    type_target_script,
)

mcp = FastMCP(
    name="agent-browser",
    instructions=(
        "Browser automation tools for the user's real Chrome/Edge session via TMWebDriver/CDP bridge. "
        "Supports page scanning, JS execution, CDP commands, screenshots, cookies, and desktop physical input. "
        "Page screenshots include MCP image content; a model that cannot process images must not claim to "
        "have seen the pixels and should use scan_page, execute_js, a page-specific API, or OCR instead. "
        "Several browsers can be connected at once; list_tabs shows a browser field per tab. "
        "Before acting, pick the target explicitly with switch_tab(browser='chrome'|'edge') or a full "
        "session_id (a 'client:tabId' string - pass it verbatim, never split it). "
        "Tabs that existed before the task are user-owned: do not close or navigate them by default. "
        "For mutating work use open_new_tab, keep its owner_id, and close that owned tab in cleanup. "
        "If a result carries status='no_response', switched_session, or bridge_error: the tab slept, "
        "reconnected, or the bridge blipped - run list_tabs, switch_tab to the right target, then retry; "
        "re-run scripts with side effects only after scan_page confirms they did not land."
    ),
)

_driver: Optional[TMWebDriver] = None
_DRIVER_PORT = int(os.environ.get("AGENT_BROWSER_TMWD_PORT", "18765"))
_DRIVER_HOST = os.environ.get("AGENT_BROWSER_TMWD_HOST", "127.0.0.1")

# The local operator profile intentionally defaults to lab. Safe remains an
# explicit, process-wide override for sessions where every foreground action
# and permission grant must be confirmed separately.
_AUTOMATION_MODE_OVERRIDE: Optional[str] = None
_AUTOMATION_MODES = frozenset({"lab", "safe"})
_DEFAULT_AUTO_BEFOREUNLOAD_HOSTS = (
    "shell.", "ttyd", "code-server", "jupyter", "vscode-web"
)
_LAB_APPROVAL_OWNERS: dict[str, Any] = {}
_LAB_PHYSICAL_APPROVALS: set[str] = set()
_LAB_SITE_PERMISSION_APPROVALS: set[str] = set()


class _TabOwnershipRegistry:
    """Process-local capabilities for tabs created by this MCP server.

    The shared bridge sees every browser tab, but each editor conversation gets
    its own MCP process and therefore its own registry.  The random owner_id is
    an additional capability check inside a process; the lifecycle generation
    prevents a reused native tab id from inheriting an earlier ownership claim.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, str]] = {}

    @staticmethod
    def _new_owner_id() -> str:
        return f"abm_owner_{secrets.token_urlsafe(18)}"

    def register(
        self,
        session_id: str,
        generation: str,
        *,
        owner_id: Optional[str] = None,
    ) -> dict[str, str]:
        sid = str(session_id)
        gen = str(generation)
        capability = str(owner_id).strip() if owner_id is not None else self._new_owner_id()
        if not capability:
            raise ValueError("owner_id must not be empty")
        record = {
            "session_id": sid,
            "generation": gen,
            "owner_id": capability,
            "opener": "agent",
        }
        with self._lock:
            self._records[sid] = record
        return dict(record)

    def validate(
        self,
        session_ids: list[str],
        *,
        owner_id: Optional[str],
        live_sessions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, str]:
        capability = str(owner_id).strip() if owner_id is not None else ""
        if not capability:
            raise PermissionError(
                "close_tabs refused: owner_id is required when only_if_agent_owned=true; "
                "use the capability returned by open_new_tab"
            )
        expected: dict[str, str] = {}
        live_by_id = {
            str(item.get("id")): item for item in (live_sessions or [])
        }
        with self._lock:
            for sid in session_ids:
                record = self._records.get(sid)
                if record is None:
                    raise PermissionError(
                        f"close_tabs refused: {sid} is not owned by this MCP task"
                    )
                if record["owner_id"] != capability:
                    raise PermissionError(
                        f"close_tabs refused: owner_id does not match {sid}"
                    )
                # A positively registered live session can reject a reused id
                # early. Absence is not proof of closure: PDF, restricted URLs,
                # and a reconnecting content script may have no session while
                # the native Chrome tab still exists.
                live = live_by_id.get(sid)
                if live is not None and live.get("generation") is not None:
                    if str(live["generation"]) != record["generation"]:
                        raise PermissionError(
                            f"close_tabs refused: {sid} lifecycle generation changed"
                        )
                expected[str(_split_session_target(sid)[1])] = record["generation"]
        return expected

    def release(self, session_ids: list[str], *, owner_id: str) -> None:
        with self._lock:
            for sid in session_ids:
                record = self._records.get(sid)
                if record and record["owner_id"] == owner_id:
                    self._records.pop(sid, None)


_TAB_OWNERSHIP = _TabOwnershipRegistry()


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _automation_mode() -> str:
    mode = (_AUTOMATION_MODE_OVERRIDE or os.environ.get("AGENT_BROWSER_MODE", "lab"))
    mode = str(mode).strip().lower()
    return mode if mode in _AUTOMATION_MODES else "lab"


def _auto_beforeunload_hosts() -> list[str]:
    raw = os.environ.get("AGENT_BROWSER_AUTO_BEFOREUNLOAD_HOSTS")
    values = raw.split(",") if raw is not None else _DEFAULT_AUTO_BEFOREUNLOAD_HOSTS
    return [str(value).strip().lower() for value in values if str(value).strip()]


def _automation_profile() -> dict[str, Any]:
    mode = _automation_mode()
    return {
        "mode": mode,
        "no_elicit": mode == "lab" and _env_enabled("AGENT_BROWSER_LAB_NO_ELICIT"),
        "auto_beforeunload_hosts": _auto_beforeunload_hosts(),
        "physical_approval": "every_action" if mode == "safe" else "once_per_session",
        "site_permission_approval": "every_allow" if mode == "safe" else "once_per_session",
    }


def _approval_key(ctx: Context) -> str:
    request_context = getattr(ctx, "request_context", None)
    owner = getattr(request_context, "session", None) or ctx
    key = f"{type(owner).__module__}.{type(owner).__qualname__}:{id(owner)}"
    # Retain the owner for the MCP process lifetime so CPython cannot recycle an
    # id and accidentally treat a different session as already approved.
    _LAB_APPROVAL_OWNERS[key] = owner
    return key


# --- Keep blocking tools off the event loop ---------------------------------
# FastMCP calls a sync tool function directly in the coroutine that handles the
# request (func_metadata: `if fn_is_async: await fn(...) else: fn(...)`), with no
# thread offload. Every tool here blocks — the bridge polls results with
# time.sleep and does synchronous HTTP — and one execute_js can chain several
# roundtrips. Run them all in a worker thread so the server keeps answering
# pings and, critically, can still process notifications/cancelled while a
# slow scan_page is in flight.
_mcp_tool = mcp.tool


def _threaded_tool(*d_args: Any, **d_kwargs: Any):
    serialize = bool(d_kwargs.pop("serialize", True))
    decorator = _mcp_tool(*d_args, **d_kwargs)

    def wrap(fn):
        if inspect.iscoroutinefunction(fn):
            return decorator(fn)

        @functools.wraps(fn)
        async def runner(*args: Any, **kwargs: Any):
            def _run():
                if not serialize:
                    return fn(*args, **kwargs)
                with _TOOL_LOCK:
                    return fn(*args, **kwargs)

            return await anyio.to_thread.run_sync(_run)

        # FastMCP builds the input schema from the signature, so runner must keep
        # the original one. `from __future__ import annotations` makes every
        # annotation a string, and pydantic resolves those against the
        # function's own globals — so carry the original signature AND the
        # defining module over, or `Optional` fails to resolve.
        # eval_str resolves the string annotations here, where Optional/Any are
        # in scope, so pydantic never has to look them up again.
        runner.__signature__ = inspect.signature(fn, eval_str=True)  # type: ignore[attr-defined]
        runner.__annotations__ = {
            name: p.annotation
            for name, p in runner.__signature__.parameters.items()
            if p.annotation is not inspect.Parameter.empty
        }
        runner.__module__ = fn.__module__
        decorator(runner)
        # Hand back the untouched sync function so in-process callers
        # (switch_session, compact_tabs, ...) don't have to await.
        return fn

    return wrap


mcp.tool = _threaded_tool  # type: ignore[assignment]


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


_SPAWN_LOCK_STALE = 30.0


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists.

    Used to recycle a spawn lock whose owner crashed mid-spawn instead of
    waiting the full _SPAWN_LOCK_STALE window — that window blocks a real
    recovery for 30s after a daemon that died seconds in.
    """
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except OSError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is not ours; treat it as alive so we don't
        # steal a lock another instance legitimately holds.
        return True
    except OSError:
        return False


def _acquire_spawn_lock() -> Optional[Path]:
    """Win the right to spawn the bridge, or return None if someone else has it.

    MCP instances start in parallel, and "is the port open? no -> spawn" is not
    atomic across processes: several instances check at the same moment, all see
    a closed port, and all spawn. The losers then sit there having lost the port
    bind. Observed for real — two daemons with identical creation timestamps.
    """
    lock = Path.home() / ".agent-browser-mcp" / "spawn.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL is the atomic part: exactly one process creates the file.
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # A crashed spawner would otherwise block every later attempt forever.
        # Two recovery paths: if the lock is older than _SPAWN_LOCK_STALE it is
        # definitely stale; otherwise read the pid and check liveness so a
        # daemon that died seconds after spawn frees the lock immediately
        # instead of holding recovery hostage for 30s.
        try:
            recycle = False
            if time.time() - lock.stat().st_mtime > _SPAWN_LOCK_STALE:
                recycle = True
            else:
                try:
                    old_pid = int(lock.read_text(encoding="utf-8").strip())
                except (ValueError, OSError):
                    old_pid = 0
                if not _pid_alive(old_pid):
                    recycle = True
            if recycle:
                lock.unlink(missing_ok=True)
                return _acquire_spawn_lock()
        except OSError:
            pass
        return None
    except OSError:
        return None
    try:
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    finally:
        os.close(fd)
    return lock


def spawn_bridge_daemon() -> bool:
    """Start the bridge as a detached process so it outlives this MCP instance.

    Returns True once the bridge HTTP port answers. Self-hosting from an MCP
    instance is avoided because these instances are spawned per session and
    recycled, taking the bridge (and its bound ports) down with them.
    """
    lock = _acquire_spawn_lock()
    if lock is None:
        # Another instance is spawning. Wait for its daemon rather than starting
        # a second one that will only lose the port bind.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _port_open(_DRIVER_HOST, _DRIVER_PORT + 1):
                return True
            time.sleep(0.25)
        return False
    ok = False
    try:
        ok = _spawn_bridge_daemon_locked()
        return ok
    finally:
        # Only release on failure. Releasing after a success would let the next
        # caller — whose own port check can still be failing, since a freshly
        # spawned daemon needs a moment to bind — win the lock and spawn a
        # duplicate. On success the lock is left to expire via _SPAWN_LOCK_STALE,
        # by which point the port is up and nobody needs to spawn at all.
        if not ok:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass


def _spawn_bridge_daemon_locked() -> bool:
    # Re-check under the lock: a daemon may have come up between the caller's
    # port check and our acquiring the lock, and spawning now would just create
    # the duplicate the lock exists to prevent.
    if _port_open(_DRIVER_HOST, _DRIVER_PORT + 1):
        return True
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
    if _driver is not None:
        return _driver
    with _DRIVER_LOCK:
        if _driver is not None:
            return _driver
        ensure_config_js()
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


def ensure_sessions(
    timeout: Optional[float] = None,
    fresh: bool = False,
    prune_default: bool = True,
) -> list[dict[str, Any]]:
    sessions = active_sessions(timeout=timeout, fresh=fresh)
    if not sessions:
        raise RuntimeError(
            "No connected browser tabs. Load the unpacked extension from the reported extension path, "
            "keep this MCP server running via Hermes, and open a normal http/https page in Chrome."
        )
    # Every session-scoped tool passes through here before handing an implicit
    # target to the driver, so this is where a dead remembered tab has to be
    # dropped — otherwise the driver falls back to it and refuses the call.
    if prune_default:
        prune_stale_default()
    return sessions


def normalize_session_id(session_id: Optional[str]) -> Optional[str]:
    if session_id is None:
        return None
    return str(session_id)


def prune_stale_default() -> Optional[str]:
    """Forget a remembered target tab that no longer exists.

    Tab ids are not stable — they change on browser restart, extension reload,
    and whenever a tab is closed — so a default session id goes stale routinely.
    Left in place it poisons every later call with "session not connected, run
    switch_tab first", which is the caller's cue to redo list_tabs + switch_tab
    for something they never chose in the first place. Re-picking is safe here
    precisely because the caller did not name a tab; an *explicit* session_id
    that is dead still raises, since substituting a different page silently is
    the worse failure.
    """
    driver = get_driver()
    cur = driver.default_session_id
    if not cur:
        return None
    if any(str(s.get("id")) == str(cur) for s in active_sessions()):
        return str(cur)
    # The session cache can simply be out of date; confirm against the bridge
    # before discarding a default that is actually fine.
    if any(str(s.get("id")) == str(cur) for s in active_sessions(fresh=True)):
        return str(cur)
    driver.default_session_id = None
    return None


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
    timeout = float(timeout)
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    driver = require_driver()
    # Pass the target session through per-call instead of mutating the global
    # default_session_id. A directed call ("run this on tab Y") must not steal
    # the shared default out from under a concurrent task working on tab X.
    # session_id=None falls back to the driver's default inside execute_js.
    sid = str(session_id) if session_id is not None else None
    first_budget = remaining()
    if first_budget <= 0:
        raise TimeoutError("bridge JS deadline exhausted before dispatch")
    response = driver.execute_js(script, timeout=first_budget, session_id=sid)
    if simphtml.no_response_kind(response) == "undelivered":
        # Never reached the page; retrying is side-effect-free.
        retry_budget = remaining()
        if retry_budget > 0:
            response = driver.execute_js(
                script,
                timeout=retry_budget,
                session_id=sid,
            )
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
        url = str(item.get("url") or "").lower()
        if ("cloudflareaccess.com" in url or
                "/cdn-cgi/access/verify" in url or
                "/cdn-cgi/access/verify-code" in url):
            item["automation_attention"] = "authentication_required"
            item["hint"] = (
                "This looks like an authentication tab opened by another process or tunnel. "
                "Complete or close it before assuming the related service is ready."
            )
        tabs.append(item)
    return tabs


# Status/diagnostic tools must answer fast even when the bridge is half-dead;
# they use this short timeout and degrade instead of raising.
_STATUS_TIMEOUT = 5.0


@mcp.tool(
    description=(
        "Return the active safe/lab automation profile. Lab is the default and may reuse one "
        "session approval; safe requires approval for every physical action and permission allow."
    )
)
def get_automation_profile() -> dict[str, Any]:
    return _automation_profile()


@mcp.tool(
    description=(
        "Set the safe or lab automation profile for this MCP process. This does not persist or "
        "reload the extension; AGENT_BROWSER_MODE controls the next process."
    )
)
def set_automation_profile(mode: str) -> dict[str, Any]:
    normalized = str(mode).strip().lower()
    if normalized not in _AUTOMATION_MODES:
        raise ValueError("mode must be one of: lab, safe")
    global _AUTOMATION_MODE_OVERRIDE
    _AUTOMATION_MODE_OVERRIDE = normalized
    _LAB_PHYSICAL_APPROVALS.clear()
    _LAB_SITE_PERMISSION_APPROVALS.clear()
    return _automation_profile()


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
        "Close one or more tabs by native tab id or composite session_id. Accepts a single "
        "identifier or a list; identifiers in one call must belong to the same browser. "
        "By default it closes only tabs created by this MCP task and requires the owner_id "
        "returned by open_new_tab; lifecycle generations are checked before removal. Set "
        "only_if_agent_owned=false only for an explicit operator request to close a user tab."
    )
)
def close_tabs(
    tab_id: int | str | list[int | str],
    session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    only_if_agent_owned: bool = True,
) -> dict[str, Any]:
    driver = require_driver()
    native_ids, client_id = _normalize_tab_targets(tab_id, session_id=session_id)
    session_ids = [f"{client_id}:{native_id}" for native_id in native_ids]
    expected_generations: dict[str, str] = {}
    if only_if_agent_owned:
        expected_generations = _TAB_OWNERSHIP.validate(
            session_ids,
            owner_id=owner_id,
            live_sessions=active_sessions(fresh=True),
        )
    requested: int | list[int] = native_ids[0] if not isinstance(tab_id, list) else native_ids
    command_ids: int | list[int] = native_ids[0] if not isinstance(tab_id, list) else native_ids
    payload: dict[str, Any] = {
        "cmd": "tabs",
        "method": "close",
        "tabId": command_ids,
    }
    if expected_generations:
        payload["expectedGenerations"] = expected_generations
    result = driver.ext_cmd(
        payload, client_id=client_id, timeout=20.0
    )
    info = _extension_data(result)
    if info.get("ok") is False:
        raise RuntimeError(info.get("error") or "the browser refused to close the tab")
    raw_closed = info.get("closed")
    raw_already_gone = info.get("alreadyGone", info.get("already_gone"))
    closed_ids = (
        [int(value) for value in raw_closed]
        if isinstance(raw_closed, list)
        else list(native_ids)
    )
    already_gone_ids = (
        [int(value) for value in raw_already_gone]
        if isinstance(raw_already_gone, list)
        else []
    )
    if only_if_agent_owned and owner_id is not None:
        _TAB_OWNERSHIP.release(session_ids, owner_id=str(owner_id))
    invalidate_sessions_cache()
    single = not isinstance(tab_id, list)
    closed: int | list[int] = (
        closed_ids[0] if single and closed_ids else [] if single else closed_ids
    )
    already_gone: int | list[int] = (
        already_gone_ids[0]
        if single and already_gone_ids
        else [] if single else already_gone_ids
    )
    status = "already_gone" if only_if_agent_owned and not closed_ids and already_gone_ids else "ok"
    closed_by = (
        "user" if status == "already_gone"
        else "agent" if only_if_agent_owned and closed_ids
        else "none"
    )
    return {
        "status": status,
        "requested": requested,
        "closed": closed,
        "already_gone": already_gone,
        "closed_by": closed_by,
        "owner_id": str(owner_id) if only_if_agent_owned else None,
        "only_if_agent_owned": bool(only_if_agent_owned),
        "result": result,
    }


@mcp.tool(
    description=(
        "Set the target tab for later calls by session id, URL substring, or browser name "
        "('chrome'/'edge'/'opera') without focusing the browser. Use activate=true or "
        "activate_tab when foreground work is required."
    )
)
def switch_tab(
    session_id: Optional[str] = None,
    url_pattern: Optional[str] = None,
    browser: Optional[str] = None,
    activate: bool = False,
) -> dict[str, Any]:
    sid = switch_session(session_id=session_id, url_pattern=url_pattern, browser=browser)
    out: dict[str, Any] = {"active_session_id": sid}
    if activate:
        try:
            out["activated"] = _activate(sid)
            time.sleep(0.3)  # let the window manager finish raising the window
        except Exception as e:
            out["activation_failed"] = str(e)
    out["tabs"] = compact_tabs()
    return out


def _activate(session_id: Optional[str] = None) -> dict[str, Any]:
    """Bring a tab to the front for real (foreground tab + focused window).

    Reports whether the tab genuinely ended up on screen. Making the tab active
    always "succeeds" even when its window is minimized, and physical input then
    lands somewhere else entirely, so the honest answer needs the window state
    rather than just the absence of an error.
    """
    driver = require_driver()
    sid = str(session_id) if session_id else driver.default_session_id
    if not sid:
        raise RuntimeError("no target session; run list_tabs then switch_tab first")
    client_id = sid.rsplit(":", 1)[0] if ":" in sid else None
    tab_id = int(sid.rsplit(":", 1)[-1])
    reply = driver.ext_cmd({"cmd": "tabs", "method": "switch", "tabId": tab_id},
                           client_id=client_id, timeout=15.0)
    out: dict[str, Any] = {"activated_session_id": sid, "tab_id": tab_id}
    # ext_cmd returns {'data': <handler reply>} both locally and over HTTP
    # (the bridge's ext_cmd wraps its own result the same way), so 'data' is
    # where onScreen actually lives. 'r' is a legacy unwrap some callers did
    # on the remote path; accept it as a fallback, never as the primary key.
    info = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(info, dict):
        info = reply.get("r") if isinstance(reply, dict) else None
    if not isinstance(info, dict):
        info = reply if isinstance(reply, dict) else {}
    # Older extension builds answer a bare {ok:true} and cannot tell us; say so
    # rather than implying the tab is on screen.
    if "onScreen" in info:
        out["on_screen"] = bool(info["onScreen"])
        out["window_state"] = info.get("windowState")
        if info.get("wasMinimized"):
            out["was_minimized"] = True
        if not info["onScreen"]:
            out["warning"] = ("window is still not on screen; screen-coordinate clicks and "
                              "desktop screenshots will not hit this tab")
    else:
        out["on_screen"] = None
        out["note"] = "extension predates window-state reporting; reload it to get this"
    return out


@mcp.tool(
    description=(
        "Bring a tab to the foreground and focus its window. Use this explicitly after "
        "switch_tab when foreground work is required, or to re-raise a tab the user has "
        "since clicked away from."
    )
)
def activate_tab(session_id: Optional[str] = None) -> dict[str, Any]:
    # Do NOT switch_session here: _activate resolves the tab from session_id
    # directly, and switching would leave the shared default parked on this tab
    # (stealing a concurrent task's target) for no benefit.
    out = _activate(session_id)
    time.sleep(0.3)  # let the window manager finish raising the window
    return {"status": "ok", **out}


def _session_url(session_id: str) -> str:
    for session in active_sessions():
        if str(session.get("id")) == str(session_id):
            return str(session.get("url") or "")
    return ""


def _lab_auto_accepts_beforeunload(
    session_id: str,
    session_url: Optional[str] = None,
) -> bool:
    if _automation_mode() != "lab":
        return False
    try:
        current_url = _session_url(session_id) if session_url is None else session_url
        host = (urlsplit(current_url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host and any(marker in host for marker in _auto_beforeunload_hosts()))


@mcp.tool(
    description=(
        "Navigate the current real-browser tab through CDP without raising its window. "
        "beforeunload defaults to dismiss, except lab mode auto-accepts configured shell/IDE "
        "hosts. Use accept to leave explicitly, manual to inspect, or intent_leave=false to "
        "force the conservative dismiss behavior even on a lab auto host."
    )
)
def open_url(
    url: str,
    session_id: Optional[str] = None,
    timeout: float = 15.0,
    beforeunload: str = "dismiss",
    intent_leave: Optional[bool] = None,
) -> dict[str, Any]:
    timeout = float(timeout)
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    deadline = time.monotonic() + timeout
    policy = _validate_dialog_policy(beforeunload)
    driver = require_driver()
    prev_default = driver.default_session_id
    session_budget = deadline - time.monotonic()
    if session_budget <= 0:
        raise TimeoutError("open_url total deadline exhausted before session resolution")
    sessions = ensure_sessions(
        timeout=session_budget,
        fresh=True,
        prune_default=False,
    )
    if deadline - time.monotonic() <= 0:
        raise TimeoutError("open_url total deadline exhausted during session resolution")
    if session_id is not None:
        requested_sid = str(session_id)
        target_session = next(
            (item for item in sessions if str(item.get("id")) == requested_sid),
            None,
        )
        if target_session is None:
            raise RuntimeError(f"Session {requested_sid} not found")
        target_sid = requested_sid
        driver.default_session_id = target_sid
    else:
        current_sid = str(prev_default) if prev_default is not None else None
        target_session = next(
            (item for item in sessions if str(item.get("id")) == current_sid),
            None,
        )
        if target_session is None:
            candidates = sessions
            preferred_browser = os.environ.get(
                "AGENT_BROWSER_PREFERRED_BROWSER", ""
            ).strip().lower()
            if preferred_browser:
                preferred = [
                    item for item in sessions
                    if str(item.get("browser", "")).lower() == preferred_browser
                ]
                if preferred:
                    candidates = preferred
            target_session = candidates[0]
        target_sid = str(target_session["id"])
        driver.default_session_id = target_sid
    client_id, tab_id = _split_session_target(target_sid)
    auto_policy = bool(
        policy == "dismiss" and intent_leave is not False
        and _lab_auto_accepts_beforeunload(
            target_sid, str(target_session.get("url") or "")
        )
    )
    effective_policy = "accept" if auto_policy else policy
    fallback_used = False
    try:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("open_url total deadline exhausted before navigation")
            response = driver.ext_cmd(
                {
                    "cmd": "navigate",
                    "tabId": tab_id,
                    "url": url,
                    "beforeunload": effective_policy,
                    "timeoutMs": max(1, int(remaining * 1000)),
                },
                client_id=client_id,
                timeout=remaining,
            )
            result = _extension_data(response)
        except Exception as route_error:
            # A timed-out navigation has unknown outcome and may already have
            # changed the page. Only an explicit unsupported-route response is
            # safe to resend through CDP.
            if not _unknown_command_error(route_error):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "open_url total deadline exhausted before CDP fallback"
                ) from route_error
            navigation = _direct_cdp(
                "Page.navigate", {"url": url}, session_id=target_sid,
                client_id=client_id, tab_id=tab_id, timeout=remaining,
                deadline=deadline,
            )
            fallback_used = True
            result = {
                "status": "ok",
                "url": url,
                "navigation": navigation,
                "bridge_route_error": str(route_error),
            }
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
        invalidate_sessions_cache()
    out = _classify_navigation_result(result, requested_url=url)
    out["active_session_id"] = target_sid
    if auto_policy:
        out["beforeunload_auto"] = True
        out["beforeunload_policy"] = "accept"
    if fallback_used:
        out["navigation_mode"] = "cdp_fallback"
    if out.get("status") in {
        "blocked_by_beforeunload", "blocked_by_dialog", "dialog_handle_failed",
        "navigation_timeout", "navigation_failed",
    }:
        leave_intended = effective_policy == "accept"
        out.setdefault(
            "hint",
            (
                "Leave intent was detected. Call resolve_leave_dialog for the bounded "
                "protocol retry and lab physical fallback."
                if leave_intended else
                "Navigation was deliberately dismissed to preserve the current page. "
                "Retry with beforeunload='accept' when leaving is intended."
            ),
        )
    return out


_DIALOG_POLICIES = frozenset({"dismiss", "accept", "manual"})


def _validate_dialog_policy(policy: Any) -> str:
    if not isinstance(policy, str) or policy not in _DIALOG_POLICIES:
        raise ValueError("dialog policy/action must be one of: dismiss, accept, manual")
    return policy


def _split_session_target(session_id: str) -> tuple[str, int]:
    sid = str(session_id)
    if ":" not in sid:
        raise ValueError(f"invalid composite session id: {sid!r}")
    client_id, raw_tab_id = sid.rsplit(":", 1)
    if not client_id:
        raise ValueError(f"invalid composite session id: {sid!r}")
    try:
        return client_id, int(raw_tab_id)
    except ValueError as exc:
        raise ValueError(f"invalid composite session id: {sid!r}") from exc


def _implicit_client_id(session_id: Optional[str] = None) -> Optional[str]:
    if session_id is not None and ":" in str(session_id):
        return str(session_id).rsplit(":", 1)[0]
    driver = require_driver()
    current = driver.default_session_id
    if current and ":" in str(current):
        return str(current).rsplit(":", 1)[0]
    try:
        sessions = active_sessions()
    except Exception:
        sessions = []
    if sessions:
        sid = str(sessions[0].get("id") or "")
        if ":" in sid:
            return sid.rsplit(":", 1)[0]
    return None


def _normalize_tab_targets(
    value: int | str | list[int | str],
    *,
    session_id: Optional[str] = None,
) -> tuple[list[int], Optional[str]]:
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ValueError("at least one tab identifier is required")
    # An explicit composite target owns the browser choice. Seeding this from
    # the shared default first made close_tabs("chrome:7") fail whenever the
    # current default happened to belong to Edge. A supplied session_id remains
    # an explicit constraint; otherwise infer the client only after parsing all
    # target values.
    client_id = _implicit_client_id(session_id) if session_id is not None else None
    native_ids: list[int] = []
    for raw in values:
        item_client: Optional[str] = None
        if isinstance(raw, bool):
            raise ValueError(f"invalid tab identifier: {raw!r}")
        if isinstance(raw, str) and ":" in raw:
            item_client, tab = _split_session_target(raw)
        else:
            try:
                tab = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid tab identifier {raw!r}; use a numeric tab id or client:tabId session_id"
                ) from exc
        if tab < 0:
            raise ValueError(f"invalid tab identifier: {raw!r}")
        if item_client:
            if client_id and client_id != item_client:
                raise ValueError("all tab identifiers in one call must belong to the same browser client")
            client_id = item_client
        native_ids.append(tab)
    if client_id is None:
        client_id = _implicit_client_id()
    return native_ids, client_id


def _unknown_command_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    return "unknown cmd" in message or "unknown command" in message


def _direct_cdp(
    method: str,
    params: dict[str, Any],
    *,
    session_id: str,
    client_id: str,
    tab_id: int,
    timeout: float,
    deadline: Optional[float] = None,
) -> Any:
    driver = require_driver()
    timeout = float(timeout)
    if timeout <= 0:
        raise TimeoutError("CDP deadline exhausted before dispatch")
    deadline = deadline if deadline is not None else time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    first_budget = remaining()
    if first_budget <= 0:
        raise TimeoutError("CDP deadline exhausted before dispatch")
    payload = {
        "cmd": "cdp",
        "method": method,
        "params": params,
        "tabId": tab_id,
        "timeoutMs": max(1, int(first_budget * 1000)),
    }
    first_error: Optional[BaseException] = None
    try:
        response = driver.ext_cmd(
            payload, client_id=client_id, timeout=first_budget
        )
    except BaseException as exc:
        first_error = exc
        fallback_budget = remaining()
        # A timed-out mutation is ambiguous: it may already be running in the
        # extension. Never send the same CDP command through a second route,
        # and never dispatch anything after the shared deadline.
        if isinstance(exc, TimeoutError) or fallback_budget <= 0:
            raise TimeoutError(
                f"CDP command did not complete within its total deadline: {exc}"
            ) from exc
        execute = getattr(driver, "execute_js", None)
        if not callable(execute):
            raise
        fallback_payload = dict(payload)
        fallback_payload["timeoutMs"] = max(1, int(fallback_budget * 1000))
        try:
            response = execute(
                json.dumps(fallback_payload),
                timeout=fallback_budget,
                session_id=session_id,
            )
        except BaseException as fallback_error:
            raise RuntimeError(
                f"CDP fallback failed after extension command error ({first_error}): "
                f"{fallback_error}. Run get_setup_status/list_tabs and restart the bridge "
                "if it reports an older command router."
            ) from fallback_error
    result = _extension_data(response)
    if result.get("ok") is False:
        code = result.get("code") or "cdp_error"
        hint = result.get("hint") or (
            "A cdp_timeout forces ABM to detach; retry once after list_tabs. "
            "A debugger_conflict requires closing DevTools or the competing debugger."
        )
        raise RuntimeError(f"{code}: {result.get('error') or 'CDP command failed'}. {hint}")
    # The real extension WS route sends res.data as the result. Therefore a
    # native CDP response such as Page.printToPDF's {data: "<base64>"} arrives
    # here as result={data:"..."}. Only unwrap an explicit extension envelope;
    # blindly unwrapping every data key destroys valid CDP payloads.
    if result.get("ok") is True and "data" in result:
        return result["data"]
    return result


def _extension_data(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return dict(data) if isinstance(data, dict) else dict(response)


def _classify_navigation_result(
    result: dict[str, Any], requested_url: str
) -> dict[str, Any]:
    out = dict(result)
    out.setdefault("requested_url", requested_url)
    out.setdefault("url", requested_url)
    navigation = out.get("navigation")
    is_download = bool(
        out.get("is_download") is True
        or out.get("isDownload") is True
        or (isinstance(navigation, dict) and navigation.get("isDownload") is True)
    )
    if is_download:
        out.update({
            "type": "download",
            "status": "triggered",
            "is_download": True,
        })
        out.setdefault(
            "hint",
            "The browser accepted this as a download. net::ERR_ABORTED can be normal "
            "when Page.navigate reports isDownload=true; use download_file for completion "
            "status and the final local path.",
        )
        return out
    dialog = out.get("dialog")
    action = out.get("dialog_action")
    terminal_statuses = {
        "navigation_timeout",
        "navigation_failed",
        "dialog_handle_failed",
        "blocked_by_beforeunload",
        "blocked_by_dialog",
    }
    if out.get("status") in terminal_statuses:
        return out
    if (out.get("handle_error") or
            (isinstance(dialog, dict) and action != "manual"
             and out.get("handled") is False)):
        out["status"] = "dialog_handle_failed"
        return out
    if isinstance(dialog, dict):
        if action == "manual":
            out["status"] = "blocked_by_dialog"
        elif dialog.get("type") == "beforeunload" and action == "dismiss":
            out["status"] = "blocked_by_beforeunload"
        else:
            out["status"] = "ok"
    else:
        landed = out.get("url")
        if isinstance(landed, str) and landed.rstrip("/") != requested_url.rstrip("/"):
            out["status"] = "redirected"
            out.setdefault(
                "note",
                "最终 URL 与请求不同，请确认是否为预期页面（可能是登录墙/重定向）。",
            )
        else:
            out.setdefault("status", "ok")
    return out


@mcp.tool(
    description=(
        "Inspect or handle a JavaScript dialog on the requested real-browser tab. "
        "action is dismiss, accept, or manual; manual reports the dialog without choosing."
    )
)
def handle_dialog(
    action: str,
    prompt_text: str = "",
    session_id: Optional[str] = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    policy = _validate_dialog_policy(action)
    driver = require_driver()
    prev_default = driver.default_session_id
    target_sid = switch_session(session_id=session_id) if session_id is not None else switch_session()
    client_id, tab_id = _split_session_target(target_sid)
    try:
        response = driver.ext_cmd(
            {
                "cmd": "handle_dialog",
                "tabId": tab_id,
                "action": policy,
                "promptText": prompt_text,
            },
            client_id=client_id,
            timeout=max(0.5, min(float(timeout), 3.0)),
        )
        result = _extension_data(response)
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    result.setdefault("status", "blocked_by_dialog" if policy == "manual" else "ok")
    result["active_session_id"] = target_sid
    if result.get("status") in {"blocked_by_dialog", "dialog_handle_failed"}:
        result.setdefault(
            "hint",
            "The dialog is still open. For an intended page leave, call resolve_leave_dialog; otherwise choose accept or dismiss explicitly.",
        )
    elif result.get("status") == "no_dialog":
        result.setdefault("url", _session_url(target_sid))
    return result


@mcp.tool(
    description=(
        "Resolve an intended beforeunload leave in one bounded workflow: protocol accept twice, "
        "then a lab-only foreground Enter fallback after the normal physical-input approval gate."
    )
)
async def resolve_leave_dialog(
    ctx: Context,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    target_sid = switch_session(session_id=session_id) if session_id is not None else switch_session()
    attempts: list[dict[str, Any]] = []
    for _ in range(2):
        try:
            result = handle_dialog("accept", session_id=target_sid, timeout=3.0)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
        attempts.append(result)
        if result.get("handled") is True or result.get("status") == "ok":
            return {
                "status": "ok",
                "resolution": "protocol",
                "session_id": target_sid,
                "attempts": attempts,
            }
        time.sleep(0.1)

    if _automation_mode() != "lab":
        return {
            "status": "requires_user_action",
            "session_id": target_sid,
            "attempts": attempts,
            "hint": "Safe mode does not send a physical leave fallback. Accept the browser dialog manually or switch to lab explicitly.",
        }

    def press_default_leave() -> dict[str, Any]:
        _pyautogui().hotkey("enter")
        return {"status": "ok", "input": "enter"}

    physical = await _run_approved_physical_action(
        ctx,
        "confirm the intended browser beforeunload leave with Enter",
        press_default_leave,
        session_id=target_sid,
        activate_session="current",
    )
    if physical.get("status") == "ok":
        return {
            "status": "ok",
            "resolution": "physical_fallback",
            "session_id": target_sid,
            "attempts": attempts,
            "physical": physical,
            "hint": "The default browser-dialog action was sent; verify the destination URL before continuing.",
        }
    return {
        "status": "requires_user_action",
        "session_id": target_sid,
        "attempts": attempts,
        "physical": physical,
        "hint": "Please click Leave/离开 in the browser dialog, then retry the intended navigation.",
    }


@mcp.tool(description="Open a real-browser tab and wait a bounded time for its exact lifecycle generation to register. A ready tab is registered as owned by this MCP task and returns owned=true plus a random owner_id capability; pass that owner_id to close_tabs during cleanup. Returns tab_id, session_id, generation, ready, URL, and load status so a reused native id cannot match a stale tab.")
def open_new_tab(
    url: str,
    timeout: float = 15.0,
    active: bool = True,
    session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> dict[str, Any]:
    driver = require_driver()
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    deadline = time.monotonic() + float(timeout)
    client_id = _implicit_client_id(session_id)
    result = driver.newtab(
        url, client_id=client_id, timeout=timeout, active=active
    )
    if isinstance(result, dict) and result.get("client_id") is not None:
        client_id = str(result["client_id"])
    info = _extension_data(result)
    if info.get("ok") is True and isinstance(info.get("data"), dict):
        info = dict(info["data"])
    raw_tab_id = info.get("id", info.get("tab_id"))
    if raw_tab_id is None:
        raise RuntimeError(f"open_new_tab did not return a native tab id: {result!r}")
    tab_id = int(raw_tab_id)
    generation = info.get("generation")
    if generation is not None:
        generation = str(generation)
    invalidate_sessions_cache()
    expected_sid = f"{client_id}:{tab_id}" if client_id else None
    found_sid: Optional[str] = None
    found_url = str(info.get("url") or url)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            sessions = active_sessions(timeout=min(2.0, remaining), fresh=True)
        except Exception:
            sessions = []
        match = next(
            (
                session for session in sessions
                if (
                    (
                        (expected_sid and str(session.get("id")) == expected_sid)
                        or (
                            not expected_sid
                            and str(session.get("id", "")).endswith(f":{tab_id}")
                        )
                    )
                    and (
                        generation is None
                        or str(session.get("generation")) == generation
                    )
                )
            ),
            None,
        )
        if match:
            found_sid = str(match.get("id"))
            found_url = str(match.get("url") or found_url)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.1, remaining))
    # Exact session+generation registration is the readiness barrier for
    # session-scoped tools.  The extension now acknowledges chrome.tabs.create
    # immediately, so its initial tab.status is commonly "loading"; retaining
    # that snapshot as a second gate would leave a permanently pending result
    # even after the content session has registered and is executable.
    ready = bool(found_sid)
    ownership: Optional[dict[str, str]] = None
    ownership_sid = found_sid or expected_sid
    if ownership_sid and generation is not None:
        ownership = _TAB_OWNERSHIP.register(
            ownership_sid,
            generation,
            owner_id=owner_id,
        )
    out: dict[str, Any] = {
        "status": "ok" if ready else "pending",
        "tab_id": tab_id,
        "session_id": found_sid or expected_sid,
        "generation": generation,
        "ready": ready,
        "url": found_url,
        "load_status": info.get("status"),
        "owned": ownership is not None,
        "opener": "agent",
        "owner_id": ownership["owner_id"] if ownership else None,
        "result": result,
    }
    if not ready:
        out["hint"] = (
            "The native tab exists but its session did not register before the bounded timeout. "
            "Use the returned tab_id with cdp_command, or list_tabs once before session tools."
        )
    elif generation is None:
        out["hint"] = (
            "The tab is ready, but the loaded extension did not return a lifecycle generation, "
            "so ABM did not mark it owned. Reload the unpacked extension before relying on safe cleanup."
        )
    return out


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


def _extension_client_id(session_id: Optional[str]) -> Optional[str]:
    return (str(session_id).rsplit(":", 1)[0]
            if session_id and ":" in str(session_id) else None)


def _extension_operation_result(
    response: Any, *, operation: str, **context: Any,
) -> dict[str, Any]:
    result = _extension_data(response)
    if result.get("ok") is False:
        return {
            "status": "error",
            "operation": operation,
            "code": result.get("code") or "extension_operation_failed",
            "error": result.get("error") or f"{operation} failed",
            **({"hint": result["hint"]} if result.get("hint") else {}),
            **context,
        }
    # The in-process/fake route commonly returns an explicit extension
    # envelope: {data: {ok: true, data: <payload>}}.  The real remote bridge,
    # however, has already removed that inner envelope in TMWebDriver.ext_cmd
    # and returns {data: <payload>} instead.  Preserve both forms.  Treating a
    # direct dict payload as an envelope used to discard capture snapshots such
    # as {status: "capturing", messages: [...]}, so live Network/Console tools
    # misleadingly returned only the generic operation status.
    if result.get("ok") is True:
        payload = result.get("data") if "data" in result else {
            key: value for key, value in result.items() if key != "ok"
        }
    else:
        payload = result
    return {
        "status": "ok",
        "operation": operation,
        **context,
        **({"data": payload} if payload not in ({}, None) else {}),
    }


def _move_download(
    source: Path,
    destination: Path,
    *,
    overwrite: bool,
) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        temporary: Optional[Path] = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".download",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as temporary_file, source.open(
                "rb"
            ) as source_file:
                shutil.copyfileobj(source_file, temporary_file)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            shutil.copystat(source, temporary)
            os.link(temporary, destination)
            temporary.unlink()
            temporary = None
            source.unlink()
            return
        except FileExistsError as exc:
            raise FileExistsError(
                f"download destination already exists: {destination}; "
                "pass overwrite=true to replace it"
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    try:
        os.replace(source, destination)
        return
    except OSError:
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{threading.get_ident()}.download"
        )
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
            source.unlink()
        finally:
            temporary.unlink(missing_ok=True)


@mcp.tool(
    description=(
        "Download an http(s) URL through the real browser's native download manager, so "
        "the current browser profile's cookies and authenticated session are used. Waits "
        "for completion by default and returns the final absolute local path. directory may "
        "be any absolute local directory; completed files are moved there without replacing an "
        "existing file unless overwrite=true. A directory timeout reports directory_applied=false "
        "because Chrome may finish in its default download directory. An explicit session_id "
        "must still be live and is never replaced with another profile. Use this for attachments "
        "instead of page fetch."
    ),
    serialize=False,
)
def download_file(
    url: str,
    filename: Optional[str] = None,
    directory: Optional[str] = None,
    wait: bool = True,
    timeout: float = 60.0,
    session_id: Optional[str] = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    parsed = urlsplit(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    timeout = float(timeout)
    if not 0 < timeout <= 1800:
        raise ValueError("timeout must be between 0 and 1800 seconds")

    relative_name: Optional[Path] = None
    if filename is not None:
        filename = str(filename).strip()
        relative_name = Path(filename)
        if (not filename or not relative_name.parts or relative_name.anchor
                or relative_name.is_absolute()
                or ".." in relative_name.parts):
            raise ValueError(
                "filename must be a non-empty relative download name without '..'"
            )

    target_directory: Optional[Path] = None
    if directory is not None:
        target_directory = Path(str(directory)).expanduser()
        if not target_directory.is_absolute():
            raise ValueError("directory must be an absolute path")
        target_directory = target_directory.resolve()
        if not wait:
            raise ValueError("directory requires wait=true")

    client_id: Optional[str] = None
    if session_id is not None:
        explicit_session_id = str(session_id)
        client_id, _ = _split_session_target(explicit_session_id)
        sessions = active_sessions(fresh=True)
        if not any(str(item.get("id")) == explicit_session_id for item in sessions):
            raise RuntimeError(f"Session {explicit_session_id} not found")
    else:
        # Pin the implicit browser while other serialized tools have their
        # temporary default-session mutations restored. Release immediately:
        # the potentially long native download wait must not block other tools.
        with _TOOL_LOCK:
            client_id = _implicit_client_id()

    payload: dict[str, Any] = {
        "cmd": "downloads",
        "method": "download",
        "url": parsed.geturl(),
        "conflictAction": "overwrite" if overwrite else "uniquify",
        "wait": bool(wait),
        "timeoutMs": max(1, int(timeout * 1000)),
    }
    if filename is not None:
        payload["filename"] = filename.replace("\\", "/")
    response = require_driver().ext_cmd(
        payload,
        client_id=client_id,
        timeout=timeout + 1.0,
    )
    result = _extension_data(response)
    if result.get("ok") is False:
        return {
            "type": "download",
            "status": "failed",
            **({"download_id": result["download_id"]} if result.get("download_id") is not None else {}),
            "error": result.get("error") or "download failed",
            **({"code": result["code"]} if result.get("code") else {}),
        }
    info = result.get("data") if result.get("ok") is True else result
    if not isinstance(info, dict):
        raise RuntimeError("download_file received an invalid extension response")
    status = str(info.get("status") or "failed")
    out: dict[str, Any] = {
        "type": "download",
        "status": status,
        **({"download_id": info["download_id"]} if info.get("download_id") is not None else {}),
        **({"bytes_received": info["bytes_received"]} if info.get("bytes_received") is not None else {}),
        **({"total_bytes": info["total_bytes"]} if info.get("total_bytes") is not None else {}),
    }
    if status == "failed":
        out["error"] = info.get("error") or "download interrupted"
        if info.get("code"):
            out["code"] = info["code"]
        if info.get("hint"):
            out["hint"] = info["hint"]
        return out
    if status != "completed":
        if target_directory is not None:
            out["directory_applied"] = False
            out["requested_directory"] = str(target_directory)
            out["hint"] = (
                "The requested directory move was not applied because the download did not "
                "finish before this call returned. The file may continue downloading into "
                "the browser's default download directory, and this call no longer tracks it."
            )
        else:
            out.setdefault(
                "hint",
                "The browser accepted the download but it did not reach a terminal state before this call returned.",
            )
        return out

    raw_path = info.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("download completed but the browser returned no local path")
    source = Path(raw_path).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(
            f"download completed but the reported local file does not exist: {source}"
        )
    final_path = source
    if target_directory is not None:
        destination_name = relative_name if relative_name is not None else Path(source.name)
        final_path = (target_directory / destination_name).resolve()
        if not final_path.is_relative_to(target_directory):
            raise ValueError("filename must stay within directory")
        _move_download(source, final_path, overwrite=bool(overwrite))
    out["path"] = str(final_path)
    out["size"] = final_path.stat().st_size
    return out


@mcp.tool(
    description=(
        "Uninstall another installed extension by id. show_confirm_dialog defaults to true; "
        "set it false only for an explicitly selected disposable/test extension. The ABM bridge "
        "cannot uninstall itself through its active connection."
    )
)
def uninstall_extension(
    extension_id: str,
    show_confirm_dialog: bool = True,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    extension_id = str(extension_id).strip()
    if not extension_id:
        raise ValueError("extension_id must not be empty")
    response = require_driver().ext_cmd(
        {
            "cmd": "management",
            "method": "uninstall",
            "extId": extension_id,
            "showConfirmDialog": bool(show_confirm_dialog),
        },
        client_id=_extension_client_id(session_id),
        timeout=20.0,
    )
    return _extension_operation_result(
        response,
        operation="uninstall_extension",
        extension_id=extension_id,
        confirmation_requested=bool(show_confirm_dialog),
    )


@mcp.tool(description="Return the browser bookmark tree. Works with no tabs open.")
def get_bookmarks(session_id: Optional[str] = None) -> dict[str, Any]:
    response = require_driver().ext_cmd(
        {"cmd": "bookmarks", "method": "tree"},
        client_id=_extension_client_id(session_id),
        timeout=30.0,
    )
    return _extension_operation_result(response, operation="get_bookmarks")


@mcp.tool(
    description=(
        "Create a bookmark or folder. Supply url for a bookmark; omit url to create a folder. "
        "parent_id is optional and uses Chrome's default bookmark location when omitted."
    )
)
def create_bookmark(
    title: str,
    url: Optional[str] = None,
    parent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    title = str(title).strip()
    if not title:
        raise ValueError("title must not be empty")
    node: dict[str, Any] = {"title": title}
    if url is not None:
        url = str(url).strip()
        if not url:
            raise ValueError("url must not be empty when supplied")
        node["url"] = url
    if parent_id is not None:
        parent_id = str(parent_id).strip()
        if not parent_id:
            raise ValueError("parent_id must not be empty when supplied")
        node["parentId"] = parent_id
    response = require_driver().ext_cmd(
        {"cmd": "bookmarks", "method": "create", "node": node},
        client_id=_extension_client_id(session_id),
        timeout=20.0,
    )
    return _extension_operation_result(response, operation="create_bookmark")


@mcp.tool(
    description=(
        "Remove a bookmark by id. Set recursive=true only for a folder whose full subtree "
        "should be removed."
    )
)
def remove_bookmark(
    bookmark_id: str,
    recursive: bool = False,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    bookmark_id = str(bookmark_id).strip()
    if not bookmark_id:
        raise ValueError("bookmark_id must not be empty")
    response = require_driver().ext_cmd(
        {
            "cmd": "bookmarks",
            "method": "removeTree" if recursive else "remove",
            "id": bookmark_id,
        },
        client_id=_extension_client_id(session_id),
        timeout=20.0,
    )
    return _extension_operation_result(
        response,
        operation="remove_bookmark",
        bookmark_id=bookmark_id,
        recursive=bool(recursive),
    )


@mcp.tool(
    description=(
        "Send a JSON message from the ABM extension service worker to another installed "
        "extension. The target must be enabled and list this ABM extension in "
        "externally_connectable. Works with no tabs open."
    )
)
def call_extension(
    extension_id: str,
    message_json: str,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    extension_id = str(extension_id).strip()
    if not extension_id:
        raise ValueError("extension_id must not be empty")
    try:
        message = json.loads(message_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"message_json must be valid JSON: {exc}") from exc
    response = require_driver().ext_cmd(
        {"cmd": "call_extension", "extId": extension_id, "message": message},
        client_id=_extension_client_id(session_id),
        timeout=20.0,
    )
    return _extension_operation_result(
        response,
        operation="call_extension",
        extension_id=extension_id,
    )


def _tab_extension_operation(
    payload: dict[str, Any],
    *,
    operation: str,
    session_id: Optional[str],
    timeout: float,
) -> dict[str, Any]:
    driver = require_driver()
    previous_default = driver.default_session_id
    try:
        if session_id is not None and ":" not in str(session_id):
            tab_ids, client_id = _normalize_tab_targets(str(session_id))
            if client_id is None:
                raise ValueError(
                    "cannot infer browser client for numeric session_id; run switch_tab first"
                )
            target_sid = f"{client_id}:{tab_ids[0]}"
        else:
            target_sid = switch_session(session_id=session_id) if session_id is not None else switch_session()
            client_id, _ = _split_session_target(target_sid)
        client_id, tab_id = _split_session_target(target_sid)
        command = {**payload, "tabId": tab_id}
        response = driver.ext_cmd(command, client_id=client_id, timeout=timeout)
    finally:
        if session_id is not None:
            driver.default_session_id = previous_default
    result = _extension_operation_result(
        response,
        operation=operation,
        session_id=target_sid,
        tab_id=tab_id,
    )
    data = result.pop("data", None)
    if result.get("status") == "ok" and isinstance(data, dict):
        result.update(data)
    elif data is not None:
        result["data"] = data
    return result


@mcp.tool(
    description=(
        "Start bounded CDP Network capture on a real-browser tab. Captures requests, responses, "
        "and optionally response bodies without foregrounding the tab. Call network_capture_stop "
        "to return the buffer and release the debugger lease."
    )
)
def network_capture_start(
    session_id: Optional[str] = None,
    include_bodies: bool = True,
    max_entries: int = 500,
    max_body_bytes: int = 262144,
    body_timeout: float = 5.0,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if not 10 <= int(max_entries) <= 2000:
        raise ValueError("max_entries must be between 10 and 2000")
    if not 1024 <= int(max_body_bytes) <= 2097152:
        raise ValueError("max_body_bytes must be between 1024 and 2097152")
    if not 0.1 <= float(body_timeout) <= 10.0:
        raise ValueError("body_timeout must be between 0.1 and 10 seconds")
    return _tab_extension_operation(
        {
            "cmd": "network_capture",
            "method": "start",
            "includeBodies": bool(include_bodies),
            "maxEntries": int(max_entries),
            "maxBodyBytes": int(max_body_bytes),
            "bodyTimeoutMs": int(float(body_timeout) * 1000),
            "timeoutMs": int(float(timeout) * 1000),
        },
        operation="network_capture_start",
        session_id=session_id,
        timeout=timeout,
    )


@mcp.tool(
    description=(
        "Stop Network capture on a real-browser tab, return all bounded request records and "
        "response bodies collected so far, and release its debugger lease."
    )
)
def network_capture_stop(
    session_id: Optional[str] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return _tab_extension_operation(
        {"cmd": "network_capture", "method": "stop"},
        operation="network_capture_stop",
        session_id=session_id,
        timeout=timeout,
    )


@mcp.tool(
    description=(
        "Start a bounded Runtime console and exception capture on a real-browser tab without "
        "foregrounding it. Use get_console_messages while running and console_capture_stop when done."
    )
)
def console_capture_start(
    session_id: Optional[str] = None,
    max_entries: int = 500,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if not 10 <= int(max_entries) <= 5000:
        raise ValueError("max_entries must be between 10 and 5000")
    return _tab_extension_operation(
        {
            "cmd": "console",
            "method": "start",
            "maxEntries": int(max_entries),
            "timeoutMs": int(float(timeout) * 1000),
        },
        operation="console_capture_start",
        session_id=session_id,
        timeout=timeout,
    )


@mcp.tool(
    description=(
        "Read a page of captured console messages and exceptions from a real-browser tab. "
        "Set clear=true to clear the full buffer after reading."
    )
)
def get_console_messages(
    session_id: Optional[str] = None,
    offset: int = 0,
    max_items: int = 200,
    clear: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if int(offset) < 0:
        raise ValueError("offset must be non-negative")
    if not 1 <= int(max_items) <= 1000:
        raise ValueError("max_items must be between 1 and 1000")
    return _tab_extension_operation(
        {
            "cmd": "console",
            "method": "get",
            "offset": int(offset),
            "maxItems": int(max_items),
            "clear": bool(clear),
        },
        operation="get_console_messages",
        session_id=session_id,
        timeout=timeout,
    )


@mcp.tool(
    description=(
        "Stop console capture on a real-browser tab, return the remaining bounded message "
        "buffer, and release its debugger lease."
    )
)
def console_capture_stop(
    session_id: Optional[str] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return _tab_extension_operation(
        {"cmd": "console", "method": "stop"},
        operation="console_capture_stop",
        session_id=session_id,
        timeout=timeout,
    )


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
    else:
        # Pin down which tab this is about before reading it. In remote mode the
        # bridge would otherwise resolve an unset target on its own, and the
        # answer came back saying active_session_id: null next to content
        # scraped from a real page — leaving the caller unable to say which tab
        # it just read, or to aim a follow-up call at the same one.
        switch_session()
    link_refs: dict[str, str] = {}
    try:
        content = simphtml.get_html(
            driver,
            cutlist=cutlist,
            maxchars=maxchars,
            instruction=instruction,
            extra_js=extra_js,
            text_only=text_only,
            timeout=timeout,
            link_refs=None if text_only else link_refs,
        )
        active = driver.default_session_id
    except simphtml.PageUnavailable as e:
        # The tab never answered. Report that as a failure with the bridge's
        # own diagnosis, instead of an empty page the agent would read as
        # "this site is blank".
        return {
            "status": "no_response",
            "active_session_id": driver.default_session_id,
            "tabs": compact_tabs(),
            "error": str(e),
        }
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    out: dict[str, Any] = {
        "status": "success",
        "active_session_id": active,
        "tabs": compact_tabs(),
        "content": content,
    }
    # Long hrefs in `content` were shortened to '#r1'-style refs; hand back the
    # real URLs so links stay usable (open_url) instead of being unreachable.
    if link_refs:
        out["links"] = {ref: url for url, ref in link_refs.items()}
    off = _offscreen_note(content)
    if off:
        out["offscreen"] = off
        if off["viewport_height"] == 0:
            # A background tab measures as zero-height, so the whole page counts
            # as "offscreen" and the numbers mean nothing. Say so rather than
            # reporting a bogus count as fact.
            out["hint"] = (
                "该标签页当前不可见（视区高度为 0），本次读取的可见性判定不可靠。"
                "请先 activate_tab 再 scan_page。"
            )
        else:
            out["hint"] = (
                f"{off['elements']} 个渲染中的元素在视区 ±5000px 之外被省略"
                f"（scrollY={off['scroll_y']}, 视区高 {off['viewport_height']}, "
                f"文档高 {off['doc_height']}）。"
                "如未找到目标，请 scroll_page 后重新 scan_page。"
            )
    return out


_OFFSCREEN_RE = re.compile(
    r"<!--tmwd-offscreen:(\d+) scrollY:(-?\d+) viewH:(\d+) docH:(\d+)-->")


def _offscreen_note(content: Any) -> Optional[dict[str, int]]:
    """Pull the optHTML offscreen marker out of the page HTML, if present."""
    if not isinstance(content, str):
        return None
    m = _OFFSCREEN_RE.search(content)
    if not m:
        return None
    return {
        "elements": int(m.group(1)),
        "scroll_y": int(m.group(2)),
        "viewport_height": int(m.group(3)),
        "doc_height": int(m.group(4)),
    }


@mcp.tool(
    description=(
        "Wait until a condition holds on the page, then return. Use this instead of "
        "polling scan_page (each scan re-serializes the whole DOM). Exactly one of "
        "selector / text / url_pattern / js must be given: selector waits for a CSS "
        "match, text for a substring in body text, url_pattern for a regex on the URL, "
        "js for a JS expression to become truthy. Polls inside the page, so it costs "
        "one bridge roundtrip regardless of how long the wait takes."
    )
)
def wait_for(
    selector: Optional[str] = None,
    text: Optional[str] = None,
    url_pattern: Optional[str] = None,
    js: Optional[str] = None,
    timeout: float = 15.0,
    gone: bool = False,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    given = [n for n, v in (("selector", selector), ("text", text),
                            ("url_pattern", url_pattern), ("js", js)) if v]
    if len(given) != 1:
        raise ValueError(
            f"pass exactly one of selector/text/url_pattern/js (got {given or 'none'})")
    kind = given[0]
    driver = require_driver()
    ensure_sessions()
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    # The condition is evaluated in-page on a 100ms interval, so a 30s wait is
    # still one roundtrip. Deadline is enforced on both sides: the page resolves
    # with timedOut, and the bridge call gets a few seconds of slack on top.
    probe = {
        "selector": "!!document.querySelector(SEL)",
        "text": "(document.body ? document.body.innerText : '').includes(SEL)",
        "url_pattern": "new RegExp(SEL).test(location.href)",
        "js": "(SEL)",
    }[kind]
    expr = probe.replace("SEL", json.dumps(selector or text or url_pattern)
                         if kind != "js" else (js or "false"))
    if gone:
        expr = f"!({expr})"
    # Wait in short in-page chunks rather than one long promise. A promise that
    # outlives its page dies with it: injected while the tab is still navigating,
    # it never resolves and the bridge reports ACK-but-no-result. Chunking means
    # an unload costs one chunk, and the next chunk lands on the new document.
    CHUNK = 4.0
    deadline = time.monotonic() + float(timeout)
    started = time.monotonic()
    info: dict[str, Any] = {}
    last_error = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = min(CHUNK, remaining)
            script = f"""
            return new Promise(resolve => {{
              const start = Date.now();
              const deadline = start + {chunk * 1000};
              const check = () => {{
                let ok = false, err = null;
                try {{ ok = !!({expr}); }} catch (e) {{ err = String(e && e.message || e); }}
                if (ok) return resolve(JSON.stringify({{met: true,
                  url: location.href, title: document.title}}));
                if (Date.now() >= deadline) return resolve(JSON.stringify({{met: false,
                  error: err, url: location.href, title: document.title,
                  ready: document.readyState}}));
                setTimeout(check, 100);
              }};
              check();
            }})
            """
            try:
                resp = exec_js(script, session_id=None, timeout=chunk + 8)
                raw = resp.get("data")
                info = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception as e:
                # Page unloaded mid-wait, or the session blinked. Waiting is
                # side-effect-free, so just try the next chunk.
                last_error = str(e)
                info = {}
                time.sleep(0.3)
                continue
            if info.get("met"):
                break
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    waited_ms = int((time.monotonic() - started) * 1000)
    met = bool(info.get("met"))
    out: dict[str, Any] = {
        "status": "success" if met else "timeout",
        "condition": f"{kind}{' gone' if gone else ''}",
        "waited_ms": waited_ms,
        "url": info.get("url"),
        "title": info.get("title"),
    }
    if not met:
        if info.get("error"):
            out["error"] = info["error"]
        elif last_error:
            out["error"] = f"页面在等待期间多次不可用：{last_error}"
        out["hint"] = "条件未在超时内满足。确认选择器/文本是否正确，或用 scan_page 查看当前页面实际内容。"
    return out


@mcp.tool(
    description=(
        "Wait for navigation to settle: blocks until the tab's URL matches url_pattern "
        "(regex, or plain substring) and — unless wait_ready=false — document.readyState is "
        "'complete', then returns the final url, title and readyState. Use this after a click "
        "or open_url that navigates; wait_for(url_pattern=...) only checks the URL and can "
        "return while the new document is still blank. Polls in-page, so a long wait is "
        "still cheap."
    )
)
def wait_for_url(
    url_pattern: str,
    timeout: float = 15.0,
    session_id: Optional[str] = None,
    wait_ready: bool = True,
) -> dict[str, Any]:
    pattern = str(url_pattern or "")
    if not pattern.strip():
        raise ValueError("url_pattern 不能为空")
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"url_pattern 不是合法正则：{e}（要按纯文本匹配请转义特殊字符）") from None
    driver = require_driver()
    ensure_sessions()
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    # 与 wait_for 同样的分块策略：一个 promise 活不过它所在的 document，导航中注入的
    # 等待会随页面卸载一起死掉、永不 resolve。分块后卸载只损失一块，下一块落在新
    # 文档里 —— 这对"等导航落定"尤其重要，因为这里本来就预期页面会换。
    # 正则匹配不上时退一步按子串匹配：调用方多半直接贴了一个 URL 进来（'?'、'.'
    # 在正则里另有含义），静默等不到不如两种都试。
    probe = (f"(new RegExp({json.dumps(pattern)}).test(location.href)"
             f" || location.href.includes({json.dumps(pattern)}))")
    if wait_ready:
        probe = f"({probe} && document.readyState === 'complete')"
    CHUNK = 4.0
    deadline = time.monotonic() + float(timeout)
    started = time.monotonic()
    info: dict[str, Any] = {}
    last_error = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = min(CHUNK, remaining)
            script = f"""
            return new Promise(resolve => {{
              const deadline = Date.now() + {chunk * 1000};
              const snap = (met) => JSON.stringify({{met, url: location.href,
                title: document.title, ready: document.readyState}});
              const check = () => {{
                let ok = false, err = null;
                try {{ ok = !!{probe}; }} catch (e) {{ err = String(e && e.message || e); }}
                if (ok) return resolve(snap(true));
                if (Date.now() >= deadline) {{
                  const out = JSON.parse(snap(false));
                  if (err) out.error = err;
                  return resolve(JSON.stringify(out));
                }}
                setTimeout(check, 100);
              }};
              check();
            }})
            """
            try:
                resp = exec_js(script, session_id=None, timeout=chunk + 8)
                raw = resp.get("data")
                info = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception as e:
                # 页面在等待中卸载，或会话眨了一下眼。等待本身没有副作用，下一块重试。
                last_error = str(e)
                info = {}
                time.sleep(0.3)
                continue
            if info.get("met"):
                break
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    met = bool(info.get("met"))
    out: dict[str, Any] = {
        "status": "success" if met else "timeout",
        "url_pattern": pattern,
        "waited_ms": int((time.monotonic() - started) * 1000),
        "url": info.get("url"),
        "title": info.get("title"),
        "ready_state": info.get("ready"),
        "waited_for_ready": bool(wait_ready),
    }
    if not met:
        if info.get("error"):
            out["error"] = info["error"]
        elif last_error:
            out["error"] = f"页面在等待期间多次不可用：{last_error}"
        landed = info.get("url")
        out["hint"] = (
            f"超时：当前 URL 是 {landed}（readyState={info.get('ready')}），与 url_pattern 不匹配"
            if landed else
            "超时且读不到当前 URL：标签页可能已休眠或断开，先 list_tabs 确认目标。")
    return out


@mcp.tool(
    description=(
        "Scroll the page and report the new position. scan_page omits anything past "
        "±5000px from the current scroll offset, so on a long page: scan, then scroll, "
        "then scan again. Pass to='bottom'/'top', a pixel offset, or a CSS selector to "
        "bring into view."
    )
)
def scroll_page(
    to: str = "bottom",
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    target = str(to).strip()
    is_selector = False
    if target.lower() in ("bottom", "end"):
        move = "window.scrollTo(0, document.documentElement.scrollHeight)"
    elif target.lower() in ("top", "start"):
        move = "window.scrollTo(0, 0)"
    elif re.fullmatch(r"[-+]?\d+(\.\d+)?", target):
        move = f"window.scrollTo(0, {float(target)})"
    else:
        is_selector = True
        move = (f"const __el = document.querySelector({json.dumps(target)});"
                f" if (!__el) return JSON.stringify({{__not_found: true}});"
                f" __el.scrollIntoView({{block: 'center'}});")
    driver = require_driver()
    ensure_sessions()
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    script = f"""
    const before = window.scrollY;
    {move}
    return new Promise(r => setTimeout(() => {{
      const de = document.documentElement;
      r(JSON.stringify({{
        before, after: Math.round(window.scrollY),
        viewH: window.innerHeight,
        docH: Math.max(de.scrollHeight, document.body.scrollHeight),
        atBottom: Math.ceil(window.scrollY + window.innerHeight) >=
                  Math.max(de.scrollHeight, document.body.scrollHeight) - 2
      }}));
    }}, 400))
    """
    try:
        resp = exec_js(script, session_id=None, timeout=timeout)
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    raw = resp.get("data")
    info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    if is_selector and info.get("__not_found"):
        return {"status": "not_found", "selector": target,
                "note": f"选择器 {target!r} 在页面中无匹配；请检查选择器，或改用 'top'/'bottom'/像素值"}
    return {
        "status": "success",
        "scrolled_from": info.get("before"),
        "scroll_y": info.get("after"),
        "viewport_height": info.get("viewH"),
        "doc_height": info.get("docH"),
        "at_bottom": info.get("atBottom"),
        "moved": info.get("before") != info.get("after"),
    }


def _build_cdp_fallback_expression(script: str, policy: str, timeout: float) -> str:
    token = f"cdp-{time.monotonic_ns()}"
    deadline_ms = max(1, min(120000, int(float(timeout) * 1000)))
    return f"""
    (async () => {{
      const rawJsCode = {json.dumps(script)}.trim();
      const policy = {json.dumps(policy)};
      const token = {json.dumps(token)};
      const deadline = Date.now() + {deadline_ms};
      const scoped = policy === 'accept' || policy === 'dismiss';
      if (scoped) {{
        const scopes = Array.isArray(window.__tmwd_dialog_scopes)
          ? window.__tmwd_dialog_scopes : [];
        scopes.push({{token, policy, deadline}});
        window.__tmwd_dialog_scopes = scopes;
        window.__tmwd_suppress_until = Math.max(
          deadline, ...scopes.map(scope => Number(scope.deadline) || 0));
      }}
      try {{
        const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
        const lines = rawJsCode.split(/\r?\n/).filter(line => line.trim());
        const lastLine = lines.length ? lines[lines.length - 1].trim() : '';
        let value;
        if (lastLine.startsWith('return')) {{
          value = await (new AsyncFunction(rawJsCode))();
        }} else {{
          try {{
            value = eval(rawJsCode);
            if (value instanceof Promise) value = await value;
          }} catch (error) {{
            if (error instanceof SyntaxError && /return/i.test(error.message))
              value = await (new AsyncFunction(rawJsCode))();
            else throw error;
          }}
        }}
        let serializable = value;
        try {{ serializable = JSON.parse(JSON.stringify(value)); }}
        catch (_) {{ serializable = String(value); }}
        return {{ok: true, data: serializable}};
      }} catch (error) {{
        return {{ok: false, error: {{name: error.name || 'Error',
          message: error.message || String(error), stack: error.stack || ''}}}};
      }} finally {{
        if (scoped && Array.isArray(window.__tmwd_dialog_scopes)) {{
          window.__tmwd_dialog_scopes = window.__tmwd_dialog_scopes
            .filter(scope => scope.token !== token && Date.now() < scope.deadline);
          window.__tmwd_suppress_until = window.__tmwd_dialog_scopes.reduce(
            (latest, scope) => Math.max(latest, Number(scope.deadline) || 0), 0);
        }}
      }}
    }})()
    """


def _execute_js_cdp_fallback(
    script: str,
    *,
    policy: str,
    target_sid: str,
    client_id: str,
    tab_id: int,
    deadline: float,
    route_error: BaseException,
) -> dict[str, Any]:
    timeout = max(0.0, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError(
            "execute_js total deadline exhausted before CDP fallback dispatch"
        ) from route_error
    evaluation = _direct_cdp(
        "Runtime.evaluate",
        {
            "expression": _build_cdp_fallback_expression(script, policy, timeout),
            "awaitPromise": True,
            "returnByValue": True,
        },
        session_id=target_sid,
        client_id=client_id,
        tab_id=tab_id,
        timeout=timeout,
        deadline=deadline,
    )
    if not isinstance(evaluation, dict):
        raise RuntimeError(f"CDP fallback returned an unexpected result: {evaluation!r}")
    if evaluation.get("exceptionDetails"):
        details = evaluation["exceptionDetails"]
        raise RuntimeError(
            str(details.get("exception", {}).get("description") or details.get("text") or details)
        )
    remote = evaluation.get("result") if isinstance(evaluation.get("result"), dict) else {}
    wrapped = remote.get("value")
    if not isinstance(wrapped, dict) or "ok" not in wrapped:
        raise RuntimeError(f"CDP fallback returned no serializable value: {evaluation!r}")
    if wrapped.get("ok") is not True:
        error = wrapped.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        return {
            "status": "failed",
            "js_return": None,
            "tab_id": tab_id,
            "execution_mode": "cdp_fallback",
            "bridge_route_error": str(route_error),
            "error": str(message or "CDP evaluation failed"),
        }
    return {
        "status": "success",
        "js_return": wrapped.get("data"),
        "tab_id": tab_id,
        "execution_mode": "cdp_fallback",
        "bridge_route_error": str(route_error),
    }


@mcp.tool(description="Execute arbitrary JS in the requested real-browser tab under one total deadline. ABM pins every monitor/retry/result roundtrip to an explicit session, uses the service-worker/page route first, and falls back to directed Runtime.evaluate on SPA/CSP bridge failures without retargeting.")
def execute_js(
    script: str,
    session_id: Optional[str] = None,
    no_monitor: bool = False,
    timeout: float = 15.0,
    dialog_policy: str = "dismiss",
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    deadline = time.monotonic() + float(timeout)

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    policy = _validate_dialog_policy(dialog_policy)
    driver = require_driver()
    session_budget = remaining()
    if session_budget <= 0:
        raise TimeoutError("execute_js total deadline exhausted before session resolution")
    sessions = ensure_sessions(
        timeout=session_budget,
        fresh=True,
        prune_default=False,
    )
    if remaining() <= 0:
        raise TimeoutError("execute_js total deadline exhausted during session resolution")
    before_sids = {str(s.get("id")) for s in sessions}
    # Point the shared default at the target only for this call's roundtrips
    # (execute_js_rich does baseline/diff/transient snapshots that read the
    # global default), then restore — a session_id-scoped call must not leave
    # the default parked on this tab and steal another task's session.
    prev_default = driver.default_session_id
    target_sid: Optional[str] = None
    if session_id is not None:
        requested_sid = str(session_id)
        if not any(str(session.get("id")) == requested_sid for session in sessions):
            raise RuntimeError(f"Session {requested_sid} not found")
        target_sid = requested_sid
    else:
        # Resolve once from the bounded snapshot. A stale implicit default may
        # be repicked; a caller-named dead session above is still refused.
        current = str(prev_default) if prev_default is not None else None
        if current and any(str(session.get("id")) == current for session in sessions):
            target_sid = current
        else:
            candidates = sessions
            preferred_browser = os.environ.get(
                "AGENT_BROWSER_PREFERRED_BROWSER", ""
            ).strip().lower()
            if preferred_browser:
                preferred = [
                    session for session in sessions
                    if str(session.get("browser", "")).lower() == preferred_browser
                ]
                if preferred:
                    candidates = preferred
            target_sid = str(candidates[0]["id"])
            driver.default_session_id = target_sid
    scope_token: Optional[str] = None
    client_id: Optional[str] = None
    tab_id: Optional[int] = None
    ext_cmd = getattr(driver, "ext_cmd", None)
    primary_error: Optional[BaseException] = None
    try:
        if target_sid is not None and callable(ext_cmd):
            client_id, tab_id = _split_session_target(target_sid)
            policy_timeout = remaining()
            policy_request: dict[str, Any] = {
                "cmd": "set_dialog_policy",
                "tabId": tab_id,
                "policy": policy,
                # Keep the wire contract stable (15.0 -> exactly 15000); the
                # ext_cmd transport below still receives only the remaining
                # end-to-end budget.
                "timeoutMs": max(1, int(float(timeout) * 1000)),
            }
            if policy == "manual":
                policy_request["source"] = script
            try:
                if policy_timeout <= 0:
                    raise TimeoutError("execute_js total deadline exhausted during policy setup")
                scope = _extension_data(ext_cmd(
                    policy_request,
                    client_id=client_id,
                    timeout=min(policy_timeout, 15.0),
                ))
            except Exception as route_error:
                if target_sid is None or client_id is None or tab_id is None:
                    raise
                if isinstance(route_error, TimeoutError) or remaining() <= 0:
                    raise TimeoutError(
                        "execute_js total deadline exhausted during policy setup"
                    ) from route_error
                if not _unknown_command_error(route_error):
                    raise
                return _execute_js_cdp_fallback(
                    script,
                    policy=policy,
                    target_sid=target_sid,
                    client_id=client_id,
                    tab_id=tab_id,
                    deadline=deadline,
                    route_error=route_error,
                )
            raw_token = scope.get("token")
            if raw_token is None:
                route_error = RuntimeError(
                    "extension did not return a dialog scope token; command router may be stale"
                )
                if remaining() <= 0:
                    raise TimeoutError(
                        "execute_js total deadline exhausted before CDP fallback dispatch"
                    ) from route_error
                return _execute_js_cdp_fallback(
                    script,
                    policy=policy,
                    target_sid=target_sid,
                    client_id=client_id,
                    tab_id=tab_id,
                    deadline=deadline,
                    route_error=route_error,
                )
            scope_token = str(raw_token)
            if not re.fullmatch(r"[A-Za-z0-9._-]+", scope_token):
                raise RuntimeError("extension returned an invalid dialog scope token")
        scoped_script = (
            script
            if policy == "manual" else
            f"/*__tmwd_dialog_scope:{scope_token}*/\n{script}"
            if scope_token is not None else script
        )
        result = simphtml.execute_js_rich(
            scoped_script,
            driver,
            no_monitor=no_monitor,
            timeout=max(0.001, remaining()),
            before_sids=before_sids,
            session_id=target_sid,
            deadline=deadline,
        )
        wrapped = result.get("js_return")
        if isinstance(wrapped, dict) and wrapped.get("__tmwd_dialog_result") is True:
            result = dict(result)
            result["js_return"] = wrapped.get("value")
            wrapped_status = wrapped.get("status")
            result["manual_blocked"] = bool(
                wrapped.get("manual_blocked")
                or wrapped_status == "blocked_by_dialog"
            )
            if isinstance(wrapped_status, str):
                result["status"] = wrapped_status
            if "handled" in wrapped:
                result["handled"] = bool(wrapped.get("handled"))
            if "pending_execution" in wrapped:
                result["pending_execution"] = bool(
                    wrapped.get("pending_execution")
                )
            if isinstance(wrapped.get("error"), dict):
                result["error"] = dict(wrapped["error"])
            dialogs = wrapped.get("dialogs")
            if isinstance(dialogs, list) and dialogs:
                result["dialogs"] = dialogs
                result["dialog"] = dialogs[-1]
                if result["manual_blocked"]:
                    result["status"] = "blocked_by_dialog"
                elif policy != "manual":
                    result["status"] = "ok"
            elif isinstance(wrapped.get("dialog"), dict):
                result["dialog"] = dict(wrapped["dialog"])
        return result
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if (scope_token is not None and tab_id is not None
                    and client_id is not None and callable(ext_cmd)):
                clear: dict[str, Any] = {"cmd": "clear_dialog_policy", "tabId": tab_id}
                if scope_token is not None:
                    clear["token"] = scope_token
                try:
                    cleanup_timeout = remaining()
                    if cleanup_timeout > 0.001:
                        ext_cmd(
                            clear,
                            client_id=client_id,
                            timeout=min(cleanup_timeout, 15.0),
                        )
                    else:
                        print("[execute_js] total deadline exhausted; dialog scope will expire naturally")
                except Exception as cleanup_error:
                    if primary_error is None:
                        raise
                    print(f"[execute_js] dialog policy cleanup failed: {cleanup_error}")
        finally:
            if session_id is not None:
                driver.default_session_id = prev_default


@mcp.tool(description="Call one Chrome DevTools Protocol command. session_id accepts client:tabId; tab_id accepts either a native number or the same composite session string.")
def cdp_command(
    method: str,
    params_json: str = "{}",
    session_id: Optional[str] = None,
    tab_id: Optional[int | str] = None,
    extension_id: Optional[str] = None,
    target_id: Optional[str] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    params = json.loads(params_json or "{}")
    payload: dict[str, Any] = {"cmd": "cdp", "method": method, "params": params}
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
        client_id = _implicit_client_id(session_id)
        return driver.ext_cmd(payload, client_id=client_id, timeout=timeout)

    driver = require_driver()
    previous_default = driver.default_session_id
    directed_sid: Optional[str] = None
    try:
        tab_id_is_composite = (
            session_id is None and isinstance(tab_id, str) and ":" in tab_id
        )
        if tab_id_is_composite:
            client_id, session_tab_id = _split_session_target(str(tab_id))
            directed_sid = str(tab_id)
        elif session_id is not None and ":" in str(session_id):
            directed_sid = switch_session(session_id=str(session_id))
            client_id, session_tab_id = _split_session_target(directed_sid)
        elif session_id is not None:
            raw_ids, client_id = _normalize_tab_targets(str(session_id))
            session_tab_id = raw_ids[0]
            if client_id is None:
                raise ValueError("cannot infer browser client for numeric session_id; run switch_tab first")
            directed_sid = f"{client_id}:{session_tab_id}"
        else:
            directed_sid = switch_session()
            client_id, session_tab_id = _split_session_target(directed_sid)

        if tab_id is not None and not tab_id_is_composite:
            tab_ids, tab_client = _normalize_tab_targets(tab_id, session_id=directed_sid)
            target_tab_id = tab_ids[0]
            if tab_client and tab_client != client_id:
                raise ValueError("tab_id and session_id identify different browser clients")
            if session_id is not None and target_tab_id != session_tab_id:
                raise ValueError("tab_id does not match the directed session_id")
        else:
            target_tab_id = session_tab_id
        data = _direct_cdp(
            method,
            params,
            session_id=directed_sid,
            client_id=client_id,
            tab_id=target_tab_id,
            timeout=timeout,
        )
        return {
            "status": "ok",
            "data": data,
            "session_id": f"{client_id}:{target_tab_id}",
            "tab_id": target_tab_id,
        }
    finally:
        if session_id is not None or tab_id is not None:
            driver.default_session_id = previous_default


@mcp.tool(
    description=(
        "Print a real-browser tab to a validated PDF file through bounded CDP. The file is "
        "written atomically only after valid non-empty PDF bytes are returned; a CDP timeout "
        "invalidates and detaches the debugger lease."
    )
)
def save_pdf(
    save_path: str,
    session_id: Optional[str] = None,
    landscape: bool = False,
    print_background: bool = True,
    prefer_css_page_size: bool = True,
    scale: float = 1.0,
    page_ranges: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    if not str(save_path).strip():
        raise ValueError("save_path must not be empty")
    if not 0.1 <= float(scale) <= 2.0:
        raise ValueError("scale must be between 0.1 and 2.0")
    if not 0.1 <= float(timeout) <= 120.0:
        raise ValueError("timeout must be between 0.1 and 120 seconds")
    params: dict[str, Any] = {
        "landscape": bool(landscape),
        "printBackground": bool(print_background),
        "preferCSSPageSize": bool(prefer_css_page_size),
        "scale": float(scale),
    }
    if page_ranges.strip():
        params["pageRanges"] = page_ranges.strip()
    result = cdp_command(
        "Page.printToPDF",
        params_json=json.dumps(params),
        session_id=session_id,
        timeout=float(timeout),
    )
    payload = result.get("data") if isinstance(result, dict) else None
    encoded = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("save_pdf failed: Page.printToPDF returned no PDF data")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise RuntimeError("save_pdf failed: Page.printToPDF returned invalid base64") from exc
    if len(raw) < 8 or not raw.startswith(b"%PDF-"):
        raise RuntimeError("save_pdf failed: decoded data is not a valid PDF document")

    path = Path(save_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "success",
        "saved_to": str(path),
        "size": len(raw),
        "session_id": result.get("session_id"),
        "tab_id": result.get("tab_id"),
        "format": "pdf",
    }


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


_PAGE_CHALLENGES = ChallengeAttemptTracker(max_attempts=3, window_seconds=120)
_PAGE_CHALLENGE_ATTEMPTS: dict[str, tuple[str, float, int]] = {}
_PAGE_CHALLENGE_LOCK = threading.Lock()


def _clear_page_challenge(session_id: str) -> None:
    with _PAGE_CHALLENGE_LOCK:
        _PAGE_CHALLENGES.clear(session_id)
        _PAGE_CHALLENGE_ATTEMPTS.pop(session_id, None)


def _prime_page_challenge(session_id: str, marker: str) -> None:
    """Install a marker baseline without counting the click as unchanged."""
    with _PAGE_CHALLENGE_LOCK:
        _PAGE_CHALLENGES.clear(session_id)
        _PAGE_CHALLENGE_ATTEMPTS[session_id] = (marker, 0.0, 0)


def _record_unchanged_page_challenge(session_id: str, marker: str) -> tuple[bool, int]:
    with _PAGE_CHALLENGE_LOCK:
        now = time.monotonic()
        previous_marker, started_at, previous_attempts = _PAGE_CHALLENGE_ATTEMPTS.get(
            session_id, (marker, 0.0, 0)
        )
        if (previous_marker != marker or previous_attempts == 0
                or now - started_at >= _PAGE_CHALLENGES.window_seconds):
            started_at = now
            previous_attempts = 0
        stalled = _PAGE_CHALLENGES.record(session_id, marker, now=now)
        attempts = previous_attempts + 1
        _PAGE_CHALLENGE_ATTEMPTS[session_id] = (marker, started_at, attempts)
        return stalled, attempts


def _blocked_page_challenge_attempts(session_id: str, marker: str) -> int | None:
    """Return the stalled attempt count while the identical marker window is live."""
    with _PAGE_CHALLENGE_LOCK:
        state = _PAGE_CHALLENGE_ATTEMPTS.get(session_id)
        if state is None:
            return None
        previous_marker, started_at, attempts = state
        now = time.monotonic()
        if now - started_at >= _PAGE_CHALLENGES.window_seconds:
            _PAGE_CHALLENGES.clear(session_id)
            _PAGE_CHALLENGE_ATTEMPTS.pop(session_id, None)
            return None
        if previous_marker != marker or attempts < _PAGE_CHALLENGES.max_attempts:
            return None
        return attempts


def _run_page_input(
    commands: list[dict[str, Any]],
    session_id: Optional[str],
    timeout: float,
    *,
    session_validated: bool = False,
) -> dict[str, Any]:
    """Dispatch one uninterrupted CDP input sequence to one resolved tab."""
    if not commands:
        raise InputValidationError("page input commands must not be empty")
    driver = require_driver()
    prev_default = driver.default_session_id
    directed = session_id is not None
    try:
        # switch_session validates a caller-named tab before any input is sent.
        # An explicit dead target must never fall back to a different live tab.
        if session_validated:
            if session_id is None:
                raise InputValidationError(
                    "session_validated requires an explicit target session"
                )
            target_session = str(session_id)
        else:
            target_session = (
                switch_session(session_id=session_id) if directed else switch_session()
            )
        payload = {"cmd": "batch", "commands": commands}
        response = exec_js(json.dumps(payload), session_id=target_session, timeout=timeout)
        return {
            "status": "success",
            "session_id": target_session,
            "input_mode": "cdp",
            "foreground_changed": False,
            "result": response.get("data") if isinstance(response, dict) else response,
        }
    finally:
        if directed:
            driver.default_session_id = prev_default


def _page_selector_info(
    selector: str,
    offset_x: float,
    offset_y: float,
    session_id: str,
    timeout: float,
) -> dict[str, Any]:
    response = exec_js(
        resolve_selector_script(selector, offset_x, offset_y),
        session_id=session_id,
        timeout=timeout,
    )
    raw = response.get("data") if isinstance(response, dict) else response
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"selector resolver returned invalid JSON: {raw!r}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"selector resolver returned an unexpected result: {raw!r}")
    return raw


def _page_type_target_info(
    selector: str,
    clear: bool,
    session_id: str,
    timeout: float,
) -> dict[str, Any]:
    response = exec_js(
        type_target_script(selector, select_all=clear),
        session_id=session_id,
        timeout=timeout,
    )
    raw = response.get("data") if isinstance(response, dict) else response
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"page_type target resolver returned invalid JSON: {raw!r}"
            ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"page_type target resolver returned an unexpected result: {raw!r}"
        )
    return raw


@mcp.tool(
    description=(
        "Click a CSS selector or viewport coordinates in a specific real browser tab using "
        "background CDP input. This does not activate the tab or move the desktop cursor."
    )
)
def page_click(
    selector: str = "",
    x: Optional[float] = None,
    y: Optional[float] = None,
    offset_x: Optional[float] = None,
    offset_y: Optional[float] = None,
    button: str = "left",
    clicks: int = 1,
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    selector_mode = isinstance(selector, str) and bool(selector)
    any_coordinate = x is not None or y is not None
    both_coordinates = x is not None and y is not None
    if any_coordinate and not both_coordinates:
        raise InputValidationError("coordinate mode requires both x and y")
    if selector_mode == both_coordinates:
        raise InputValidationError(
            "page_click requires exactly one targeting mode: selector, or both x and y"
        )

    if not selector_mode:
        out = _run_page_input(
            click_commands(x, y, button=button, clicks=clicks),  # type: ignore[arg-type]
            session_id,
            timeout,
        )
        out["target"] = {"x": x, "y": y}
        return out

    driver = require_driver()
    prev_default = driver.default_session_id
    directed = session_id is not None
    try:
        target_session = switch_session(session_id=session_id) if directed else switch_session()
        resolver_x = 0 if offset_x is None else offset_x
        resolver_y = 0 if offset_y is None else offset_y
        before = _page_selector_info(
            selector, resolver_x, resolver_y, target_session, timeout
        )
        if not before.get("found"):
            _clear_page_challenge(target_session)
            return {
                "status": "not_found",
                "session_id": target_session,
                "input_mode": "cdp",
                "foreground_changed": False,
                "challenge_detected": False,
                "attempts": 0,
                "target": {"selector": selector},
            }

        resolved_x = before.get("x")
        resolved_y = before.get("y")
        if offset_x is None:
            resolved_x += before.get("width", 0) / 2
        if offset_y is None:
            resolved_y += before.get("height", 0) / 2
        before_marker = before.get("challengeMarker")
        if before_marker is not None:
            blocked_attempts = _blocked_page_challenge_attempts(
                target_session, str(before_marker)
            )
            if blocked_attempts is not None:
                return {
                    "status": "challenge_stalled",
                    "session_id": target_session,
                    "input_mode": "cdp",
                    "foreground_changed": False,
                    "challenge_detected": True,
                    "attempts": blocked_attempts,
                    "target": {
                        "selector": selector,
                        "x": resolved_x,
                        "y": resolved_y,
                        "offset_x": offset_x,
                        "offset_y": offset_y,
                    },
                    "next_action": (
                        "Stop automatic attempts and let the user take over the same tab; "
                        f"resume with session_id={target_session!r} after the challenge clears."
                    ),
                }
        out = _run_page_input(
            click_commands(resolved_x, resolved_y, button=button, clicks=clicks),
            target_session,
            timeout,
        )
        out["target"] = {
            "selector": selector,
            "x": resolved_x,
            "y": resolved_y,
            "offset_x": offset_x,
            "offset_y": offset_y,
        }

        after = _page_selector_info(
            selector, resolver_x, resolver_y, target_session, timeout
        )
        after_marker = after.get("challengeMarker") if after.get("found") else None
        if after_marker:
            after_marker = str(after_marker)
            out["challenge_detected"] = True
            if before_marker is None or str(before_marker) != after_marker:
                _prime_page_challenge(target_session, after_marker)
                out["attempts"] = 0
                return out
            stalled, attempts = _record_unchanged_page_challenge(
                target_session, after_marker
            )
            out["attempts"] = attempts
            if stalled:
                out.update({
                    "status": "challenge_stalled",
                    "next_action": (
                        "Stop automatic attempts and let the user take over the same tab; "
                        f"resume with session_id={target_session!r} after the challenge clears."
                    ),
                })
        else:
            _clear_page_challenge(target_session)
            out["challenge_detected"] = False
            out["attempts"] = 0
        return out
    finally:
        if directed:
            driver.default_session_id = prev_default


@mcp.tool(
    description=(
        "Insert text into the focused element or a CSS-selected field in a specific tab using "
        "background CDP input; xterm containers automatically retarget their helper textarea. "
        "Optionally clear the field and submit a key. A missing/unusable target returns "
        "not_found without dispatching text or key events."
    )
)
def page_type(
    text: str,
    selector: str = "",
    clear: bool = False,
    submit_key: str = "",
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    if not isinstance(text, str):
        raise InputValidationError("text must be a string")
    if not isinstance(selector, str):
        raise InputValidationError("selector must be a string")
    if not isinstance(clear, bool):
        raise InputValidationError("clear must be a boolean")
    if not isinstance(submit_key, str):
        raise InputValidationError("submit_key must be a string")
    timeout = float(timeout)
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    deadline = time.monotonic() + timeout
    driver = require_driver()
    previous_default = driver.default_session_id
    directed = session_id is not None
    try:
        session_budget = max(0.0, deadline - time.monotonic())
        if session_budget <= 0:
            raise TimeoutError("page_type deadline exhausted before session resolution")
        sessions = ensure_sessions(
            timeout=session_budget,
            fresh=True,
            prune_default=False,
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("page_type deadline exhausted during session resolution")
        if directed:
            requested_sid = str(session_id)
            if not any(
                str(session.get("id")) == requested_sid for session in sessions
            ):
                raise RuntimeError(f"Session {requested_sid} not found")
            target_session = requested_sid
        else:
            current = (
                str(previous_default) if previous_default is not None else None
            )
            if current and any(
                str(session.get("id")) == current for session in sessions
            ):
                target_session = current
            else:
                candidates = sessions
                preferred_browser = os.environ.get(
                    "AGENT_BROWSER_PREFERRED_BROWSER", ""
                ).strip().lower()
                if preferred_browser:
                    preferred = [
                        session for session in sessions
                        if str(session.get("browser", "")).lower()
                        == preferred_browser
                    ]
                    if preferred:
                        candidates = preferred
                target_session = str(candidates[0]["id"])
                driver.default_session_id = target_session
        resolution_budget = max(0.0, deadline - time.monotonic())
        if resolution_budget <= 0:
            raise TimeoutError("page_type deadline exhausted before target resolution")
        target_info = _page_type_target_info(
            selector,
            clear,
            target_session,
            resolution_budget,
        )
        target = {"selector": selector} if selector else {"focused_element": True}
        if not target_info.get("found"):
            return {
                "status": "not_found",
                "session_id": target_session,
                "input_mode": "cdp",
                "foreground_changed": False,
                "target": target,
                "target_kind": target_info.get("targetKind", "missing"),
                "typed_chars": 0,
            }
        input_budget = max(0.0, deadline - time.monotonic())
        if input_budget <= 0:
            raise TimeoutError("page_type deadline exhausted before input dispatch")
        # The resolver above focused/selected the exact sink. Dispatch only the
        # trusted text/key portion after it positively identified a target.
        commands = type_commands(
            selector,
            text,
            select_all=clear,
            submit_key=submit_key or None,
        )[1:]
        out = _run_page_input(
            commands,
            target_session,
            input_budget,
            session_validated=True,
        )
        out["target"] = target
        out["target_kind"] = target_info.get("targetKind", "element")
        out["typed_chars"] = len(text)
        return out
    finally:
        if directed:
            driver.default_session_id = previous_default


@mcp.tool(
    description=(
        "Press a key or comma-delimited modifier chord in a specific tab using background CDP "
        "input, without activating the tab."
    )
)
def page_press(
    keys_csv: str,
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    out = _run_page_input(press_commands(keys_csv), session_id, timeout)
    out["target"] = {"keys_csv": keys_csv}
    return out


@mcp.tool(
    description=(
        "Drag between viewport coordinates in a specific tab using one background CDP input "
        "sequence, without activating the tab or moving the desktop cursor."
    )
)
def page_drag(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    duration: float = 0.3,
    button: str = "left",
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    out = _run_page_input(
        drag_commands(x1, y1, x2, y2, duration=duration, button=button),
        session_id,
        timeout,
    )
    out["target"] = {"from": [x1, y1], "to": [x2, y2]}
    return out


@mcp.tool(
    description=(
        "Set files on a file input, which JS cannot do (input.files is read-only). "
        "Give a CSS selector for the <input type=file> and absolute local paths. Runs as a "
        "single CDP batch so the DOM node ids stay valid across the sequence."
    )
)
def upload_files(
    selector: str,
    paths: str | list[str],
    session_id: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    files = [paths] if isinstance(paths, str) else list(paths)
    missing = [p for p in files if not Path(p).is_file()]
    if missing:
        raise RuntimeError(f"file(s) not found: {missing}")
    files = [str(Path(p).resolve()) for p in files]
    # One batch, one attach: DOM.getDocument's nodeId is only valid while the
    # debugger stays attached, so this cannot be split across cdp_command calls.
    batch = {
        "cmd": "batch",
        "commands": [
            {"cmd": "cdp", "method": "DOM.getDocument", "params": {"depth": -1}},
            {"cmd": "cdp", "method": "DOM.querySelector",
             "params": {"nodeId": "$0.root.nodeId", "selector": selector}},
            {"cmd": "cdp", "method": "DOM.setFileInputFiles",
             "params": {"nodeId": "$1.nodeId", "files": files}},
        ],
    }
    result = exec_js(json.dumps(batch), session_id=session_id, timeout=timeout)
    # The extension's batch reply arrives as a BARE ARRAY — ws.onmessage does
    # `res.data ?? res.results ?? res` and handleBatch returns {ok, results},
    # so `data` here is the results list, not a wrapper dict. Only the error
    # path (handleBatch catch) surfaces as {ok:false, error, results}.
    data = result.get("data")
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(f"upload failed: {data.get('error')}")
    results = data if isinstance(data, list) else (
        data.get("results") if isinstance(data, dict) else None)
    node = None
    if isinstance(results, list) and len(results) > 1 and isinstance(results[1], dict):
        node = results[1].get("nodeId")
        if not node:
            raise RuntimeError(
                f"selector {selector!r} matched no element (DOM.querySelector returned "
                f"{results[1]}); check the selector and that the input is in the top frame")
    return {"status": "ok", "selector": selector, "files": files, "node_id": node}


@mcp.tool(description="Get cookies for the current page or specified tab via the Chrome extension bridge.")
def get_cookies(session_id: Optional[str] = None, tab_id: Optional[int] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"cmd": "cookies"}
    if tab_id is not None:
        payload["tabId"] = tab_id
    return exec_js(json.dumps(payload), session_id=session_id, timeout=15.0)


# --- cookie 写入 -------------------------------------------------------------
# 读走扩展的 chrome.cookies（get_cookies），写走 CDP Network.setCookie：CDP 能写
# HttpOnly / 跨路径 / 指定 domain，页面内的 document.cookie 一样都做不到。CDP 不可
# 用时（debugger 被占、页面禁止 attach）才退到 document.cookie，且必须说清降级后
# 哪些字段丢了 —— 静默丢掉 HttpOnly 会让调用方以为写进去了。
_SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None",
             "no_restriction": "None", "unspecified": None}


def _parse_cookies_arg(cookies: Any) -> list[dict[str, Any]]:
    """接受 JSON 文本 / 单个 dict / dict 列表，统一成列表。"""
    if isinstance(cookies, str):
        text = cookies.strip()
        if not text:
            raise ValueError("cookies 为空")
        try:
            cookies = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"cookies 不是合法 JSON：{e}") from None
    if isinstance(cookies, dict):
        cookies = [cookies]
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("cookies 必须是非空的 cookie 对象或对象列表")
    return cookies


def _normalize_cookie(raw: Any, index: int) -> dict[str, Any]:
    """校验并转成 CDP Network.setCookie 的参数形状。"""
    where = f"cookies[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 不是对象：{type(raw).__name__}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"{where} 缺少 name")
    # 名字里带 '=' 或 ';' 会把 Cookie 头拆坏，CDP 也不会替你挡。
    if any(ch in name for ch in "=;,\r\n \t"):
        raise ValueError(f"{where} name 含非法字符（= ; , 空白 换行）：{name!r}")
    out: dict[str, Any] = {"name": name, "value": str(raw.get("value", ""))}
    if any(ch in out["value"] for ch in ";\r\n"):
        raise ValueError(f"{where} value 含非法字符（; 或换行）；请自行 encodeURIComponent")
    for key in ("url", "domain", "path"):
        val = raw.get(key)
        if val not in (None, ""):
            out[key] = str(val)
    # 同时收 httponly / http_only 这些写法：agent 常混着传，静默忽略等于静默丢标志。
    for key, aliases in (("httpOnly", ("httpOnly", "httponly", "http_only")),
                         ("secure", ("secure",))):
        val = next((raw[a] for a in aliases if a in raw), None)
        if val is not None:
            out[key] = bool(val)
    expires = raw.get("expires", raw.get("expirationDate"))
    if expires not in (None, ""):
        try:
            out["expires"] = float(expires)
        except (TypeError, ValueError):
            raise ValueError(f"{where} expires 必须是 Unix 秒时间戳，收到 {expires!r}") from None
    same = raw.get("sameSite", raw.get("same_site"))
    if same not in (None, ""):
        key = str(same).strip().lower()
        if key not in _SAMESITE:
            raise ValueError(
                f"{where} sameSite 只能是 Strict/Lax/None，收到 {same!r}")
        norm = _SAMESITE[key]
        if norm is not None:
            out["sameSite"] = norm
    # SameSite=None 没有 Secure 会被浏览器整条丢掉，且不报错 —— 提前挡住。
    if out.get("sameSite") == "None" and not out.get("secure"):
        raise ValueError(f"{where} sameSite='None' 必须同时 secure=true，否则浏览器会静默丢弃该 cookie")
    return out


def _page_location(session_id: Optional[str] = None,
                   timeout: float = 10.0) -> dict[str, Any]:
    """当前页的 location，用于给没写 url/domain 的 cookie 补上作用域。"""
    resp = exec_js(
        "JSON.stringify({url: location.href, origin: location.origin,"
        " host: location.hostname, protocol: location.protocol,"
        " path: location.pathname})",
        session_id=session_id, timeout=timeout)
    raw = resp.get("data")
    info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return info if isinstance(info, dict) else {}


_SITE_PERMISSION_CONTENT_SETTINGS = {
    "notifications": "notifications",
    "geolocation": "location",
    "location": "location",
    "camera": "camera",
    "microphone": "microphone",
}
_SITE_PERMISSION_SETTINGS = {"allow", "block", "ask"}


def _normalize_site_permission_origin(raw_origin: Any) -> str:
    if not isinstance(raw_origin, str) or not raw_origin.strip():
        raise ValueError("origin must be an http or https origin")
    try:
        parsed = urlsplit(raw_origin.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin must be an http or https origin") from exc
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("origin must be an http or https origin")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _validate_site_permission_duration(duration_seconds: Any) -> int:
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or not 60 <= duration_seconds <= 600:
        raise ValueError("duration_seconds must be an integer between 60 and 600")
    return duration_seconds


def _site_permission_spec(permission: Any) -> dict[str, str]:
    if not isinstance(permission, str):
        raise ValueError("unsupported permission")
    name = permission.strip().lower()
    if name == "clipboard":
        return {"kind": "clipboard", "setting": "clipboard"}
    content_setting = _SITE_PERMISSION_CONTENT_SETTINGS.get(name)
    if content_setting is None:
        raise ValueError("unsupported permission")
    return {"kind": "content", "setting": content_setting}


def _validate_site_permission_setting(setting: Any) -> str:
    if not isinstance(setting, str) or setting not in _SITE_PERMISSION_SETTINGS:
        raise ValueError("setting must be one of: allow, block, ask")
    return setting


class SitePermissionApproval(BaseModel):
    approve: StrictBool = Field(description="Approve this temporary site permission")


async def _request_site_permission_approval(
    ctx: Context, permission: str, origin: str, duration_seconds: int
) -> bool:
    profile = _automation_profile()
    if profile["mode"] == "lab" and profile["no_elicit"]:
        return True
    approval_key = _approval_key(ctx)
    if profile["mode"] == "lab" and approval_key in _LAB_SITE_PERMISSION_APPROVALS:
        return True
    try:
        result = await ctx.elicit(
            message=("ABM requests temporary site permission: "
                     f"allow {permission} for {origin} for {duration_seconds} seconds"),
            schema=SitePermissionApproval,
        )
        approved = (
            result.action == "accept" and result.data is not None
            and result.data.approve is True
        )
        if approved and profile["mode"] == "lab":
            _LAB_SITE_PERMISSION_APPROVALS.add(approval_key)
        return approved
    except Exception:
        return False


def _site_permission_requires_user_action() -> dict[str, Any]:
    return {
        "status": "requires_user_action",
        "message": "Temporary site-permission approval was declined, cancelled, or unavailable.",
    }


def _site_permission_extension_result(response: Any) -> dict[str, Any]:
    result = _extension_data(response)
    classified = dict(result)
    if result.get("unsupported"):
        classified["status"] = "unsupported"
        classified["message"] = str(result.get("error") or "browser API unavailable")
        return classified
    if result.get("ok") is False:
        classified["status"] = "error"
        classified["message"] = str(result.get("error") or "site permission failed")
        return classified
    return classified


@mcp.tool(
    description=(
        "Temporarily set an origin-scoped browser site permission for 60-600 seconds. "
        "Only http/https origins and notifications, geolocation/location, camera, microphone, or clipboard are supported. "
        "safe asks on every allow; lab asks once per MCP session or skips prompts only when "
        "AGENT_BROWSER_LAB_NO_ELICIT=1. All leases restore their prior setting on expiry."
    )
)
async def set_site_permission(
    ctx: Context,
    permission: str,
    setting: str,
    origin: str = "",
    duration_seconds: int = 300,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    spec = _site_permission_spec(permission)
    normalized_setting = _validate_site_permission_setting(setting)
    duration = _validate_site_permission_duration(duration_seconds)
    driver = require_driver()
    previous_default = driver.default_session_id
    target_sid = switch_session(session_id=session_id) if session_id is not None else switch_session()
    client_id, tab_id = _split_session_target(target_sid)
    try:
        selected_origin = origin or str(_page_location(target_sid).get("url") or "")
        normalized_origin = _normalize_site_permission_origin(selected_origin)
        if normalized_setting == "allow" and not await _request_site_permission_approval(
            ctx, spec["setting"], normalized_origin, duration
        ):
            return _site_permission_requires_user_action()
        response = driver.ext_cmd(
            {
                "cmd": "site_permission",
                "action": "set",
                "tabId": tab_id,
                "permission": spec["setting"],
                "setting": normalized_setting,
                "origin": normalized_origin,
                "durationSeconds": duration,
            },
            client_id=client_id,
            timeout=20.0,
        )
        result = _site_permission_extension_result(response)
        result.setdefault("origin", normalized_origin)
        result.setdefault("permission", spec["setting"])
        result.setdefault("duration_seconds", duration)
        if result.get("status") not in {"unsupported", "error"}:
            result.setdefault("status", "ok")
        return result
    finally:
        if session_id is not None:
            driver.default_session_id = previous_default


@mcp.tool(
    description=(
        "Restore matching temporary site-permission leases now. Omit origin and permission to reset every "
        "lease for the selected browser; origin accepts only http/https."
    )
)
def reset_site_permissions(
    origin: str = "",
    permission: str = "",
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    normalized_origin = _normalize_site_permission_origin(origin) if origin else ""
    spec = _site_permission_spec(permission) if permission else None
    driver = require_driver()
    previous_default = driver.default_session_id
    target_sid = switch_session(session_id=session_id) if session_id is not None else switch_session()
    client_id, tab_id = _split_session_target(target_sid)
    try:
        response = driver.ext_cmd(
            {
                "cmd": "site_permission",
                "action": "reset",
                "tabId": tab_id,
                "origin": normalized_origin,
                "permission": spec["setting"] if spec else "",
            },
            client_id=client_id,
            timeout=20.0,
        )
        result = _site_permission_extension_result(response)
        result.setdefault("origin", normalized_origin)
        if spec:
            result.setdefault("permission", spec["setting"])
        if result.get("status") not in {"unsupported", "error"}:
            result.setdefault("status", "ok")
        return result
    finally:
        if session_id is not None:
            driver.default_session_id = previous_default


def _cdp(method: str, params: dict[str, Any], session_id: Optional[str],
         tab_id: Optional[int], timeout: float) -> Any:
    payload: dict[str, Any] = {"cmd": "cdp", "method": method, "params": params}
    if tab_id is not None:
        payload["tabId"] = tab_id
    return exec_js(json.dumps(payload), session_id=session_id,
                   timeout=timeout).get("data")


def _cookie_via_document(cookie: dict[str, Any], session_id: Optional[str],
                         timeout: float) -> dict[str, Any]:
    """降级路径：页面内 document.cookie，写完立刻回读确认。

    document.cookie 写不了 HttpOnly，也写不了别的域，所以这里只报"写没写进去"
    这一件事实，剩下的差异原样交回给调用方。
    """
    script = f"""
    const c = {json.dumps(cookie)};
    // 写和回读用同一个 encode 后的名字：非 ASCII 名字写进去是 %XX，拿原名去
    // 匹配会读不到，然后把一次成功的写入报成失败。
    const n = encodeURIComponent(c.name);
    const parts = [n + '=' + encodeURIComponent(c.value || '')];
    parts.push('path=' + (c.path || '/'));
    if (c.domain) parts.push('domain=' + c.domain);
    if (c.expires) parts.push('expires=' + new Date(c.expires * 1000).toUTCString());
    if (c.secure) parts.push('secure');
    if (c.sameSite) parts.push('samesite=' + c.sameSite);
    try {{ document.cookie = parts.join('; '); }}
    catch (e) {{ return JSON.stringify({{ok: false, error: String(e && e.message || e)}}); }}
    const re = new RegExp('(?:^|; )' + n.replace(/([.*+?^${{}}()|[\\]\\\\])/g, '\\\\$1') + '=');
    return JSON.stringify({{ok: re.test(document.cookie), cookie_header_len: document.cookie.length}});
    """
    resp = exec_js(script, session_id=session_id, timeout=timeout)
    raw = resp.get("data")
    info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return info if isinstance(info, dict) else {}


@mcp.tool(
    description=(
        "Write cookies into the real browser profile. Takes one cookie object or a list "
        "(JSON text is accepted): name is required, plus optional value/url/domain/path/"
        "expires (Unix seconds)/httpOnly/secure/sameSite. Uses CDP Network.setCookie so "
        "HttpOnly and cross-path cookies work; falls back to document.cookie only if CDP "
        "is unavailable, and then says which cookies could not carry HttpOnly. Cookies "
        "with neither url nor domain are scoped to the current page."
    )
)
def set_cookies(
    cookies: str | list | dict,
    session_id: Optional[str] = None,
    tab_id: Optional[int] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    items = [_normalize_cookie(c, i) for i, c in enumerate(_parse_cookies_arg(cookies))]
    page: dict[str, Any] = {}
    if any("url" not in c and "domain" not in c for c in items):
        page = _page_location(session_id=session_id, timeout=min(timeout, 10.0))
        if not page.get("url"):
            raise RuntimeError(
                "无法读取当前页 URL 来确定 cookie 作用域；请在每个 cookie 里显式给 url 或 domain。")
    results: list[dict[str, Any]] = []
    for cookie in items:
        params = dict(cookie)
        scoped_to_page = False
        if "url" not in params and "domain" not in params:
            params["url"] = page["url"]
            scoped_to_page = True
        entry: dict[str, Any] = {"name": params["name"], "method": "cdp"}
        if scoped_to_page:
            entry["scoped_to"] = params["url"]
        try:
            data = _cdp("Network.setCookie", params, session_id, tab_id, timeout)
            # 老版本协议回 {success: bool}，新版本回 {} —— 没有该字段就按成功算，
            # 真失败时 handleCDP 会抛 error，走不到这里。
            ok = data.get("success", True) if isinstance(data, dict) else True
            entry["status"] = "ok" if ok else "failed"
            if not ok:
                entry["error"] = "CDP Network.setCookie 返回 success=false（通常是 domain/secure 与当前页不匹配）"
        except Exception as e:
            entry["cdp_error"] = str(e)
            entry["method"] = "document.cookie"
            if cookie.get("httpOnly"):
                entry["httpOnly_dropped"] = True
            if tab_id is not None:
                # document.cookie runs in the DEFAULT tab, not the named one —
                # writing there would report ok for a cookie that never reached
                # the target. Fail loudly instead of lying.
                entry["status"] = "failed"
                entry["error"] = (
                    f"CDP 不可用（{e}）且指定了 tab_id={tab_id}；document.cookie 降级"
                    "只能作用于默认标签页，无法安全降级，请去掉 tab_id 或用 session_id 重试"
                )
            else:
                try:
                    fb = _cookie_via_document(params, session_id, timeout)
                    entry["status"] = "ok" if fb.get("ok") else "failed"
                    if not fb.get("ok"):
                        entry["error"] = (
                            fb.get("error")
                            or "document.cookie 写入后回读不到：可能被 domain/secure 限制或浏览器拒绝")
                    elif cookie.get("httpOnly"):
                        entry["note"] = "已降级为 document.cookie 写入，HttpOnly 无法通过页面 JS 设置，该 cookie 不是 HttpOnly"
                except Exception as e2:
                    entry["status"] = "failed"
                    entry["error"] = f"CDP 与 document.cookie 都失败：{e2}"
        results.append(entry)
    ok_count = sum(1 for r in results if r.get("status") == "ok")
    status = "ok" if ok_count == len(results) else ("partial" if ok_count else "failed")
    out: dict[str, Any] = {
        "status": status,
        "set": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }
    if status != "ok":
        out["hint"] = "部分 cookie 未写入。用 get_cookies 核对实际结果，并确认 domain/secure/sameSite 与目标站点一致。"
    return out


@mcp.tool(
    description=(
        "Delete a cookie by name from the real browser profile. Scope defaults to the "
        "current page (url), or pass domain/path/url to target another scope. Uses CDP "
        "Network.deleteCookies, falling back to expiring it via document.cookie."
    )
)
def delete_cookies(
    name: str,
    domain: Optional[str] = None,
    path: Optional[str] = None,
    url: Optional[str] = None,
    session_id: Optional[str] = None,
    tab_id: Optional[int] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    cookie_name = str(name or "").strip()
    if not cookie_name:
        raise ValueError("name 不能为空")
    params: dict[str, Any] = {"name": cookie_name}
    if url:
        params["url"] = str(url)
    if domain:
        params["domain"] = str(domain)
    if path:
        params["path"] = str(path)
    scoped_to_page = "url" not in params and "domain" not in params
    if scoped_to_page:
        page = _page_location(session_id=session_id, timeout=min(timeout, 10.0))
        if not page.get("url"):
            raise RuntimeError("无法读取当前页 URL 来确定删除范围；请显式给 url 或 domain。")
        params["url"] = page["url"]
    out: dict[str, Any] = {"name": cookie_name, "scope": {k: v for k, v in params.items() if k != "name"},
                           "method": "cdp"}
    try:
        _cdp("Network.deleteCookies", params, session_id, tab_id, timeout)
        out["status"] = "ok"
    except Exception as e:
        out["cdp_error"] = str(e)
        out["method"] = "document.cookie"
        if tab_id is not None:
            # The expiration script runs in the default tab; expiring cookies
            # there lies about the named tab's cookies. Fail loudly instead.
            out["status"] = "failed"
            out["error"] = (
                f"CDP 不可用（{e}）且指定了 tab_id={tab_id}；document.cookie 过期法"
                "只能作用于默认标签页，无法安全降级，请去掉 tab_id 或用 session_id 重试"
            )
            return out
        script = f"""
        const c = {json.dumps({'name': cookie_name, 'domain': params.get('domain'), 'path': params.get('path')})};
        const n = encodeURIComponent(c.name);
        // 删除必须 path/domain 全中才生效，调用方常常两个都没给：把当前路径和
        // 裸/点两种 domain 写法都试一遍，比让它"删了但还在"好。
        const paths = c.path ? [c.path] : ['/', location.pathname];
        const domains = c.domain ? [c.domain] : [null, location.hostname, '.' + location.hostname];
        for (const p of paths) for (const d of domains) {{
          let s = n + '=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=' + p;
          if (d) s += '; domain=' + d;
          try {{ document.cookie = s; }} catch (_) {{}}
        }}
        const re = new RegExp('(?:^|; )' + n.replace(/([.*+?^${{}}()|[\\]\\\\])/g, '\\\\$1') + '=');
        return JSON.stringify({{gone: !re.test(document.cookie)}});
        """
        try:
            resp = exec_js(script, session_id=session_id, timeout=timeout)
            raw = resp.get("data")
            info = json.loads(raw) if isinstance(raw, str) else (raw or {})
            out["status"] = "ok" if info.get("gone") else "failed"
            if not info.get("gone"):
                out["error"] = "过期法执行后仍能读到该 cookie：可能是 HttpOnly 或作用域不匹配，请显式给 domain/path"
        except Exception as e2:
            out["status"] = "failed"
            out["error"] = f"CDP 与 document.cookie 都失败：{e2}"
    return out


# --- localStorage / sessionStorage -------------------------------------------
_STORAGE_AREAS = {"local": "localStorage", "session": "sessionStorage",
                  "localstorage": "localStorage", "sessionstorage": "sessionStorage"}
# 整个 localStorage 可能有几 MB，原样回给 agent 会把上下文烧光。
_STORAGE_DUMP_LIMIT = 20000


def _storage_area(area: str) -> str:
    key = str(area or "").strip().lower()
    if key not in _STORAGE_AREAS:
        raise ValueError(f"area 只能是 'local' 或 'session'，收到 {area!r}")
    return _STORAGE_AREAS[key]


@mcp.tool(
    description=(
        "Read localStorage or sessionStorage. Give a key for one value, or omit it to dump "
        "every key (values are truncated past ~20k chars and truncated is reported). "
        "area='local' (default) or 'session'."
    )
)
def storage_get(
    key: Optional[str] = None,
    area: str = "local",
    session_id: Optional[str] = None,
    timeout: float = 30.0,
    offset: int = 0,
    max_items: int = 500,
    max_bytes: int = _STORAGE_DUMP_LIMIT,
) -> dict[str, Any]:
    store = _storage_area(area)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 5000:
        raise ValueError("max_items must be an integer between 1 and 5000")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 5_000_000:
        raise ValueError("max_bytes must be an integer between 1 and 5000000")
    script = f"""
    try {{
      const s = window.{store};
      if (!s) return JSON.stringify({{ok: false, error: '{store} 不可用'}});
      const key = {json.dumps(key)};
      if (key !== null) {{
        const v = s.getItem(key);
        return JSON.stringify({{ok: true, found: v !== null, value: v}});
      }}
      const items = {{}};
      const offset = {offset}, maxItems = {max_items}, maxBytes = {max_bytes};
      let bytes = 0, truncated = false, nextOffset = null, emitted = 0;
      for (let i = offset; i < s.length; i++) {{
        const k = s.key(i);
        const v = s.getItem(k) || '';
        const itemBytes = new Blob([k, v]).size;
        if (emitted >= maxItems || bytes + itemBytes > maxBytes) {{
          truncated = true; nextOffset = i; break;
        }}
        items[k] = v;
        bytes += itemBytes;
        emitted += 1;
      }}
      return JSON.stringify({{ok: true, items, total_keys: s.length, truncated,
        next_offset: nextOffset, bytes}});
    }} catch (e) {{
      return JSON.stringify({{ok: false, error: String(e && e.message || e)}});
    }}
    """
    try:
        resp = exec_js(script, session_id=session_id, timeout=timeout)
        raw = resp.get("data")
        info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception as exc:
        is_timeout = isinstance(exc, TimeoutError) or "timeout" in str(exc).lower()
        return {
            "status": "error",
            "area": store,
            "key": key,
            "error_code": "timeout" if is_timeout else "bridge_error",
            "error": str(exc),
            "retryable": True,
            "hint": "The storage call failed in-page; the MCP connection remains usable. Run list_tabs, then retry the same directed session once.",
        }
    if not isinstance(info, dict):
        return {"status": "error", "area": store, "error_code": "invalid_response",
                "error": f"页面返回了意外结果：{info!r}", "retryable": True}
    if not info.get("ok"):
        return {"status": "error", "area": store, "error_code": "storage_unavailable",
                "error": info.get("error") or "存储不可访问",
                "retryable": False,
                "hint": "第三方 Cookie/存储被拦截或页面是 sandbox/data: 上下文时会这样，换正常 http(s) 页面重试。"}
    out: dict[str, Any] = {"status": "success", "area": store}
    if key is not None:
        out["key"] = key
        out["found"] = bool(info.get("found"))
        out["value"] = info.get("value")
        if not out["found"]:
            out["note"] = "该键不存在（value 为 null，与存了空字符串不同）"
        return out
    out["items"] = info.get("items") or {}
    out["count"] = len(out["items"])
    out["total_keys"] = info.get("total_keys")
    out["offset"] = offset
    out["bytes"] = info.get("bytes", 0)
    if info.get("truncated"):
        out["truncated"] = True
        out["next_offset"] = info.get("next_offset")
        out["hint"] = "Storage output hit max_items or max_bytes; continue with next_offset or read a single key."
    else:
        out["truncated"] = False
    return out


@mcp.tool(
    description=(
        "Write one key into localStorage or sessionStorage and read it back to confirm. "
        "area='local' (default) or 'session'. Values are strings; non-string values are "
        "JSON-encoded first."
    )
)
def storage_set(
    key: str,
    value: str,
    area: str = "local",
    session_id: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    store = _storage_area(area)
    if not isinstance(key, str) or not key:
        raise ValueError("key 必须是非空字符串")
    encoded = False
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
        encoded = True
    script = f"""
    try {{
      const s = window.{store};
      if (!s) return JSON.stringify({{ok: false, error: '{store} 不可用'}});
      const k = {json.dumps(key)}, v = {json.dumps(value)};
      const existed = s.getItem(k) !== null;
      s.setItem(k, v);
      // 回读确认：配额满 / 隐私模式下 setItem 可能抛，也可能静默不落盘。
      return JSON.stringify({{ok: s.getItem(k) === v, existed, keys: s.length}});
    }} catch (e) {{
      return JSON.stringify({{ok: false, error: String(e && e.message || e)}});
    }}
    """
    try:
        resp = exec_js(script, session_id=session_id, timeout=timeout)
        raw = resp.get("data")
        info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception as exc:
        is_timeout = isinstance(exc, TimeoutError) or "timeout" in str(exc).lower()
        return {
            "status": "error",
            "area": store,
            "key": key,
            "error_code": "timeout" if is_timeout else "bridge_error",
            "error": str(exc),
            "retryable": True,
            "hint": "The write failed without closing MCP. Confirm with storage_get before retrying because a timed-out write may have landed.",
        }
    if not isinstance(info, dict) or not info.get("ok"):
        err = info.get("error") if isinstance(info, dict) else None
        return {
            "status": "failed", "area": store, "key": key,
            "error": err or "写入后回读不一致：可能超出存储配额或被浏览器拦截",
            "hint": "确认页面是正常 http(s) 页、未开无痕/未拦截站点数据；超配额时先删掉无用键。",
        }
    out: dict[str, Any] = {
        "status": "success", "area": store, "key": key,
        "bytes": len(value), "replaced": bool(info.get("existed")),
        "total_keys": info.get("keys"),
    }
    if encoded:
        out["note"] = "非字符串值已 JSON 序列化后写入"
    return out


@mcp.tool(
    description=(
        "Capture a screenshot of the current page/tab via CDP. Returns text metadata plus an "
        "attached MCP image even when save_path is set; save_path only controls disk output. "
        "If the current model cannot consume images, it has not seen the pixels and must use "
        "scan_page, execute_js, a page-specific API, or OCR instead. Base64 is included only "
        "when return_base64=true."
    )
)
def capture_page_screenshot(
    session_id: Optional[str] = None,
    tab_id: Optional[int] = None,
    format: str = "png",
    save_path: str = "",
    return_base64: bool = False,
) -> CallToolResult:
    driver = require_driver()
    previous_default = driver.default_session_id
    try:
        if session_id is not None:
            target_sid = switch_session(session_id=session_id)
        else:
            # Match the implicit execute_js path: a stale remembered default may
            # be replaced, while a caller-directed dead session must hard-fail.
            ensure_sessions()
            target_sid = switch_session()
        client_id, session_tab_id = _split_session_target(target_sid)
        target_tab_id = int(tab_id) if tab_id is not None else session_tab_id
        if session_id is not None and target_tab_id != session_tab_id:
            raise ValueError(
                f"tab_id {target_tab_id} does not match directed session_id {target_sid!r}"
            )

        payload: dict[str, Any] = {
            "cmd": "cdp",
            "method": "Page.captureScreenshot",
            "params": {"format": format},
            "tabId": target_tab_id,
        }
        response = driver.ext_cmd(payload, client_id=client_id, timeout=20.0)
        result = _extension_data(response)
    finally:
        if session_id is not None:
            driver.default_session_id = previous_default
    data = result.get("data")
    if isinstance(data, dict) and "data" in data:
        b64 = data["data"]
    else:
        b64 = data
    if not isinstance(b64, str) or not b64:
        raise RuntimeError(
            f"截图失败：桥没返回图片数据（data={data!r}）。"
            "确认目标 tab 是真实页面且未被调试器占用，或先用 list_tabs 确认。")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise RuntimeError("截图失败：桥返回了无效的 base64 图片数据。") from exc

    out: dict[str, Any] = {
        "status": "success",
        "format": format,
        "size": len(raw),
        "image_attached": True,
        "model_note": (
            "Screenshot pixels are attached as MCP image content. If the current model does not "
            "support images, it has not seen those pixels and must not infer page state from this "
            "result; use scan_page, execute_js, a page-specific API, or OCR."
        ),
    }
    if save_path:
        path = Path(save_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        out["saved_to"] = str(path)
    if return_base64:
        out["base64"] = b64

    # Keep base64 out of the text block even when explicitly requested. It
    # remains available in structuredContent for machine consumers, while the
    # model receives the actual pixels through ImageContent.
    text_metadata = {key: value for key, value in out.items() if key != "base64"}
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(text_metadata, ensure_ascii=False)),
            MCPImage(data=raw, format=format).to_image_content(),
        ],
        structuredContent=out,
    )


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


class PhysicalInputApproval(BaseModel):
    approve: StrictBool = Field(description="Approve this one physical input action")


async def _request_physical_approval(ctx: Context, summary: str) -> bool:
    """Ask the client for approval without touching any physical-input APIs."""
    profile = _automation_profile()
    if profile["mode"] == "lab" and profile["no_elicit"]:
        return True
    approval_key = _approval_key(ctx)
    if profile["mode"] == "lab" and approval_key in _LAB_PHYSICAL_APPROVALS:
        return True
    try:
        result = await ctx.elicit(
            message=f"ABM requests one physical input action: {summary}",
            schema=PhysicalInputApproval,
        )
        approved = (
            result.action == "accept" and result.data is not None
            and result.data.approve is True
        )
        if approved and profile["mode"] == "lab":
            _LAB_PHYSICAL_APPROVALS.add(approval_key)
        return approved
    except Exception:
        # Older MCP clients may not implement elicitation. Physical input is
        # deliberately unavailable in that case instead of silently proceeding.
        return False


def _requires_user_action() -> dict[str, Any]:
    return {
        "status": "requires_user_action",
        "message": "One-action physical-input approval was declined, cancelled, or unavailable.",
    }


def _physical_error_result(status: str, message: str) -> dict[str, Any]:
    return {"status": status, "message": message}


async def _run_approved_physical_action(
    ctx: Context,
    summary: str,
    action: Callable[[], dict[str, Any]],
    *,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = None,
) -> dict[str, Any]:
    if not await _request_physical_approval(ctx, summary):
        return _requires_user_action()

    should_activate = session_id is not None or activate_session not in (None, "none")
    action_started = False

    def worker() -> dict[str, Any]:
        nonlocal action_started
        with _TOOL_LOCK:
            def gated_action() -> dict[str, Any]:
                nonlocal action_started
                # Activation is itself foreground work and must wait until the
                # lease and quiet-input check have passed.
                action_started = False
                activated = None
                if should_activate:
                    activated = _maybe_activate(activate_session, session_id)
                    if not isinstance(activated, dict) or activated.get("on_screen") is not True:
                        return {
                            "status": "activation_failed",
                            "message": (
                                "The requested browser target could not be confirmed on screen; "
                                "no physical input was sent."
                            ),
                            "activated": activated,
                        }
                action_started = True
                result = action()
                if activated:
                    result["activated"] = activated
                return result

            return physical_input.run_physical_action(summary, gated_action)

    try:
        return await anyio.to_thread.run_sync(worker)
    except physical_input.PhysicalInputBusy as exc:
        if action_started:
            raise
        return _physical_error_result("busy", str(exc))
    except physical_input.InputActivityDetected as exc:
        if action_started:
            raise
        return _physical_error_result("input_activity_detected", str(exc))


_PHYSICAL_INPUT_NOTICE = (
    " Safe mode requires one-action approval; lab reuses one session approval or skips prompting "
    "when AGENT_BROWSER_LAB_NO_ELICIT=1. Approval may foreground the selected browser tab. "
    "For browser-targeted input, pass an explicit session_id (preferred)."
)


@mcp.tool(description="Move the real mouse cursor to screen coordinates." + _PHYSICAL_INPUT_NOTICE)
async def mouse_move(ctx: Context, x: int, y: int, duration: float = 0.0) -> dict[str, Any]:
    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        pyautogui.moveTo(x, y, duration=duration)
        return {"status": "ok", "x": x, "y": y}

    return await _run_approved_physical_action(ctx, f"move cursor to ({x}, {y})", action)


def _maybe_activate(activate_session: Optional[str],
                    session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Raise the target tab before a screen-coordinate action.

    Physical input lands on whatever is actually on screen, so skipping this
    makes a switch_tab + mouse_click pair click the previously visible tab —
    silently, since the coordinates are valid and pyautogui reports success.
    That is why raising the tab is the default and opting out is explicit.

    ``session_id`` wins when given, and is the parameter to reach for: every
    other tool here takes one, and the shared global default is not a safe
    stand-in for it. Session-scoped tools save and restore that default, so an
    agent that carefully passes session_id to scan_page and then calls this
    without one gets whatever tab some *other* task last selected — the more
    disciplined the caller, the more surprising the miss.

    Otherwise ``None``/``"current"`` raise the current target tab, a session id
    in ``activate_session`` raises that tab, and ``"none"`` skips activation for
    genuine desktop clicks outside the browser.
    """
    if session_id is None and activate_session == "none":
        return None
    target = session_id
    if target is None and activate_session not in (None, "current"):
        target = activate_session
    try:
        info = _activate(target)
        time.sleep(0.3)
        return info
    except Exception as e:
        # No target tab yet is normal for a desktop click; don't fail the action.
        return {"activation_skipped": str(e)}


@mcp.tool(
    description=(
        "Click on the real desktop at screen coordinates. Pass session_id — the same one you "
        "pass every other tool (preferred) — and that tab is raised after the quiet check so "
        "the click lands on it. "
        "Without one the current global target is raised, which another task may have "
        "changed. Approval may foreground the selected browser tab. "
        "activate_session='none' clicks the desktop as-is."
    )
)
async def mouse_click(
    ctx: Context,
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.1,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = "current",
) -> dict[str, Any]:
    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        if x is not None and y is not None:
            pyautogui.click(x=x, y=y, clicks=clicks, interval=interval, button=button)
        else:
            pyautogui.click(clicks=clicks, interval=interval, button=button)
        return {"status": "ok", "x": x, "y": y, "button": button, "clicks": clicks}

    target = f" at ({x}, {y})" if x is not None and y is not None else " at the current pointer"
    return await _run_approved_physical_action(
        ctx,
        f"click{target} with {button} button",
        action,
        session_id=session_id,
        activate_session=activate_session,
    )


@mcp.tool(description="Drag the real mouse from one point to another." + _PHYSICAL_INPUT_NOTICE)
async def mouse_drag(
    ctx: Context,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration: float = 0.3,
    button: str = "left",
) -> dict[str, Any]:
    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        pyautogui.moveTo(x1, y1)
        pyautogui.dragTo(x2, y2, duration=duration, button=button)
        return {"status": "ok", "from": [x1, y1], "to": [x2, y2], "button": button}

    return await _run_approved_physical_action(ctx, f"drag from ({x1}, {y1}) to ({x2}, {y2})", action)


@mcp.tool(
    description=(
        "Type text via the real keyboard, optionally after clicking a field. Pass session_id — "
        "the same one you pass every other tool (preferred) — and that tab is raised after the "
        "quiet check so the keystrokes go to it. Without one the current global target is raised, "
        "which another task may have changed. Approval may foreground the selected browser tab. "
        "activate_session='none' types into whatever already has focus."
    )
)
async def type_text(
    ctx: Context,
    text: str,
    interval: float = 0.01,
    click_x: Optional[int] = None,
    click_y: Optional[int] = None,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = "current",
) -> dict[str, Any]:
    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        if click_x is not None and click_y is not None:
            pyautogui.click(click_x, click_y)
            time.sleep(0.1)
        pyautogui.write(text, interval=interval)
        return {"status": "ok", "typed_chars": len(text)}

    return await _run_approved_physical_action(
        ctx,
        f"type {len(text)} characters",
        action,
        session_id=session_id,
        activate_session=activate_session,
    )


@mcp.tool(description="Send a hotkey chord like 'command,l' or 'ctrl,shift,p' via the real keyboard." + _PHYSICAL_INPUT_NOTICE)
async def hotkey(ctx: Context, keys_csv: str) -> dict[str, Any]:
    keys = [k.strip() for k in keys_csv.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("keys_csv must contain at least one key")

    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        pyautogui.hotkey(*keys)
        return {"status": "ok", "keys": keys}

    return await _run_approved_physical_action(ctx, f"send hotkey {keys_csv}", action)


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
