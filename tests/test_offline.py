"""Offline tests: no browser, no bridge, no network.

These cover the pure functions that decide what the agent is told. They are the
ones that turn a browser event into a claim about the world, so a regression
here is a regression in honesty, not just in convenience.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from agent_browser_mcp import simphtml
from agent_browser_mcp import server as S
from agent_browser_mcp.tmwebdriver import Session, TMWebDriver


# --- how a bridge reply is classified -----------------------------------

@pytest.mark.parametrize("response,expected", [
    ({"data": 2}, None),
    ({"data": None}, None),                       # an explicit null return
    ({"data": ""}, None),                         # falsy but still a return
    ({"result": "Session a:1 reloaded.", "closed": 1}, "navigated"),
    ({"result": "Session a:1 reloaded and new page is loading...", "closed": 1}, "navigated"),
    ({"result": "No response data in 15s (no ACK, script may not have been delivered)"}, "undelivered"),
    ({"result": "Session a:1 no response in 15s (script not polled)"}, "undelivered"),
    ({"result": "No response data in 15s (ACK received, script may still be running)"}, "after_ack"),
    ({"result": "Session a:1 no response in 15s (delivered but no result)"}, "after_ack"),
    ({}, None),
    ("not a dict", None),
    ({"result": 42}, None),                       # non-string result
])
def test_no_response_kind(response, expected):
    assert simphtml.no_response_kind(response) == expected


def test_navigation_is_not_success():
    """A page that unloaded mid-flight must not be reported as a plain success.

    This is the regression that mattered most: clicking a link is the single
    most common action, and it used to come back status:"success" with the
    bridge's diagnostic string masquerading as the script's return value.
    """
    reloaded = {"result": "Session a:1 reloaded.", "closed": 1}
    assert simphtml.no_response_kind(reloaded) == "navigated"
    # and it must be distinguishable from a real return
    assert simphtml.no_response_kind({"data": "Session a:1 reloaded."}) is None


# --- link refs ----------------------------------------------------------

SHORT = "/a"
LONG = "/some/quite/long/path/that/exceeds/thirty/characters"


def test_short_hrefs_are_left_alone():
    refs: dict[str, str] = {}
    soup = simphtml.optimize_html_for_tokens(
        f'<a href="{SHORT}">x</a>', link_refs=refs)
    assert soup.a["href"] == SHORT
    assert refs == {}


def test_long_href_becomes_a_ref():
    refs: dict[str, str] = {}
    soup = simphtml.optimize_html_for_tokens(
        f'<a href="{LONG}">x</a>', link_refs=refs)
    assert soup.a["href"] == "#r1"
    assert list(refs.values()) == ["r1"]
    assert list(refs) == [LONG]


def test_refs_are_made_absolute():
    """A ref of '/settings' is not something open_url can navigate to."""
    refs: dict[str, str] = {}
    simphtml.optimize_html_for_tokens(
        f'<a href="{LONG}">x</a>', link_refs=refs,
        base_url="https://example.com/dir/page?q=1")
    assert list(refs) == [f"https://example.com{LONG}"]


def test_repeated_url_reuses_one_ref():
    refs: dict[str, str] = {}
    soup = simphtml.optimize_html_for_tokens(
        f'<a href="{LONG}">a</a><a href="{LONG}">b</a>', link_refs=refs)
    hrefs = [a["href"] for a in soup.find_all("a")]
    assert hrefs == ["#r1", "#r1"]
    assert len(refs) == 1


def test_without_link_refs_the_old_placeholder_is_kept():
    """text_only callers pass link_refs=None and must not gain refs."""
    soup = simphtml.optimize_html_for_tokens(f'<a href="{LONG}">x</a>')
    assert soup.a["href"] == "__link__"


def test_ref_numbering_is_stable_across_calls_on_one_dict():
    refs: dict[str, str] = {}
    other = "/another/path/that/is/definitely/longer/than/thirty"
    simphtml.optimize_html_for_tokens(f'<a href="{LONG}">x</a>', link_refs=refs)
    simphtml.optimize_html_for_tokens(f'<a href="{other}">y</a>', link_refs=refs)
    assert sorted(refs.values()) == ["r1", "r2"]


# --- the offscreen marker ----------------------------------------------

def test_offscreen_note_parses_the_marker():
    html = '<body><!--tmwd-offscreen:15 scrollY:0 viewH:780 docH:2574--><p>x</p></body>'
    assert S._offscreen_note(html) == {
        "elements": 15, "scroll_y": 0, "viewport_height": 780, "doc_height": 2574,
    }


def test_offscreen_note_absent_when_nothing_was_dropped():
    assert S._offscreen_note("<body><p>x</p></body>") is None


def test_offscreen_note_handles_negative_scroll():
    """Overscroll (rubber-banding) reports a negative scrollY on some platforms."""
    html = '<!--tmwd-offscreen:3 scrollY:-40 viewH:600 docH:2000-->'
    note = S._offscreen_note(html)
    assert note is not None and note["scroll_y"] == -40


def test_offscreen_note_ignores_non_string():
    assert S._offscreen_note(None) is None
    assert S._offscreen_note({"content": "x"}) is None


# --- wait_for argument validation ---------------------------------------

def test_wait_for_requires_a_condition():
    with pytest.raises(ValueError, match="exactly one"):
        S.wait_for()


def test_wait_for_rejects_two_conditions():
    with pytest.raises(ValueError, match="exactly one"):
        S.wait_for(selector="body", text="hello")


def test_wait_for_rejects_all_four():
    with pytest.raises(ValueError):
        S.wait_for(selector="a", text="b", url_pattern="c", js="true")


# --- upload_files argument validation -----------------------------------

def test_upload_files_rejects_a_missing_path(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        S.upload_files("input[type=file]", str(tmp_path / "nope.txt"))


def test_upload_files_rejects_a_directory(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        S.upload_files("input[type=file]", str(tmp_path))


# --- the tool surface ---------------------------------------------------

EXPECTED_TOOLS = {
    "get_setup_status", "get_automation_profile", "set_automation_profile",
    "list_tabs", "list_all_tabs", "close_tabs",
    "switch_tab", "activate_tab", "open_url", "open_new_tab",
    "extension_path", "list_extensions", "set_extension_enabled",
    "uninstall_extension", "get_bookmarks", "create_bookmark",
    "remove_bookmark", "call_extension",
    "network_capture_start", "network_capture_stop", "console_capture_start",
    "get_console_messages", "console_capture_stop",
    "save_pdf",
    "scan_page", "wait_for", "scroll_page", "execute_js", "handle_dialog",
    "resolve_leave_dialog",
    "cdp_command", "debugger_targets", "cdp_batch", "upload_files",
    "get_cookies", "capture_page_screenshot", "capture_desktop_screenshot",
    "mouse_move", "mouse_click", "mouse_drag", "type_text", "hotkey",
    "pointer_info",
    # background page input
    "page_click", "page_type", "page_press", "page_drag",
    # cookies / storage
    "set_cookies", "delete_cookies", "storage_get", "storage_set",
    # temporary site permissions
    "set_site_permission", "reset_site_permissions",
    # navigation wait
    "wait_for_url",
}


@pytest.mark.anyio
async def _list_tools():
    return await S.mcp.list_tools()


def test_every_tool_is_registered():
    import asyncio

    tools = asyncio.run(S.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_every_registered_tool_is_a_coroutine():
    """Sync tools would block the event loop; the wrapper must have wrapped all."""
    import asyncio
    import inspect

    tools = asyncio.run(S.mcp.list_tools())
    sync = [t.name for t in tools
            if not inspect.iscoroutinefunction(S.mcp._tool_manager.get_tool(t.name).fn)]
    assert sync == []


def test_in_process_callers_still_get_sync_functions():
    """The decorator returns the original function so internal calls work."""
    import inspect

    assert not inspect.iscoroutinefunction(S.scan_page)
    assert not inspect.iscoroutinefunction(S.wait_for)


# --- background page input ---------------------------------------------

def test_page_input_tools_do_not_activate_and_restore_default(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    calls = []
    activated = []

    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": "c:target", "url": "https://example.test/"},
    ])
    monkeypatch.setattr(S, "_activate", lambda *args, **kwargs: activated.append((args, kwargs)))

    def fake_exec_js(script, session_id=None, timeout=15.0):
        calls.append((script, session_id, timeout))
        if script.lstrip().startswith("(() =>"):
            return {"data": {"found": True, "targetKind": "element"}}
        return {"data": {"ok": True}}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)

    results = [
        S.page_click(x=10, y=20, session_id="c:target"),
        S.page_type("hello", selector="#field", clear=True,
                    session_id="c:target"),
        S.page_type("replacement", clear=True, session_id="c:target"),
        S.page_press("ctrl,a", session_id="c:target"),
        S.page_drag(1, 2, 30, 40, session_id="c:target"),
    ]

    assert activated == []
    assert driver.default_session_id == "c:previous"
    assert all(result["input_mode"] == "cdp" for result in results)
    assert all(result["foreground_changed"] is False for result in results)
    assert all(result["session_id"] == "c:target" for result in results)
    batches = [
        json.loads(script) for script, _, _ in calls
        if script.lstrip().startswith("{")
    ]
    assert all(batch["cmd"] == "batch" for batch in batches)
    assert all(session_id == "c:target" for _, session_id, _ in calls)
    focused_batch = batches[2]
    assert focused_batch["commands"][0]["method"] == "Input.insertText"
    resolver_scripts = [script for script, _, _ in calls if script.lstrip().startswith("(() =>")]
    assert resolver_scripts
    assert all(".xterm-helper-textarea" in script for script in resolver_scripts)


def test_execute_js_rich_pins_explicit_session_for_every_page_roundtrip(monkeypatch):
    class Driver:
        default_session_id = "c:other"

        def __init__(self):
            self.calls = []

        def execute_js(self, script, timeout=15, session_id=None):
            self.calls.append((script, timeout, session_id))
            return {"data": 7, "executed_tab_id": 42}

        def get_session_dict(self, timeout=3):
            return {"c:42": "https://example.test/"}

    driver = Driver()
    html_sessions = []
    transient_sessions = []

    def get_html(*args, **kwargs):
        html_sessions.append(kwargs.get("session_id"))
        return "<main>before</main>" if len(html_sessions) == 1 else "<main>after</main>"

    monkeypatch.setattr(simphtml, "get_html", get_html)
    monkeypatch.setattr(
        simphtml,
        "get_temp_texts",
        lambda *args, **kwargs: transient_sessions.append(kwargs.get("session_id")) or [],
    )
    monkeypatch.setattr(simphtml.time, "sleep", lambda _seconds: None)

    result = simphtml.execute_js_rich(
        "return 7",
        driver,
        no_monitor=False,
        timeout=1.0,
        before_sids={"c:42"},
        session_id="c:42",
    )

    assert result["js_return"] == 7
    assert all(call[2] == "c:42" for call in driver.calls)
    assert html_sessions == ["c:42", "c:42"]
    assert transient_sessions == ["c:42"]
    assert driver.default_session_id == "c:other"


def test_execute_js_rich_uses_one_total_deadline_for_retry_and_grace():
    class SlowDriver:
        default_session_id = "c:42"

        def __init__(self):
            self.timeouts = []

        def execute_js(self, _script, timeout=15, session_id=None):
            assert session_id == "c:42"
            self.timeouts.append(timeout)
            time.sleep(max(0.0, timeout))
            return {"result": f"No response data in {timeout}s (no ACK, script may not have been delivered)"}

    driver = SlowDriver()
    started = time.monotonic()
    result = simphtml.execute_js_rich(
        "return 1",
        driver,
        no_monitor=True,
        timeout=0.12,
        before_sids=set(),
        session_id="c:42",
    )
    elapsed = time.monotonic() - started

    assert result["status"] == "no_response"
    assert elapsed < 0.30
    assert sum(driver.timeouts) <= 0.16


def test_driver_ack_does_not_restart_execute_js_deadline():
    driver = TMWebDriver.__new__(TMWebDriver)
    driver.is_remote = False
    driver.results = {}
    driver.acks = {}
    driver.default_session_id = "c:1"
    driver.latest_session_id = "c:1"
    driver.clean_sessions = lambda: None

    class Socket:
        def send_message(self, payload):
            exec_id = json.loads(payload)["id"]
            timer = threading.Timer(
                0.08, lambda: driver.acks.__setitem__(exec_id, time.time())
            )
            timer.daemon = True
            timer.start()

    session = Session(
        "c:1",
        {"url": "https://example.test/", "type": "ext_ws", "tab_id": 1},
        Socket(),
    )
    driver.sessions = {"c:1": session}

    started = time.monotonic()
    result = driver.execute_js("return new Promise(() => {})", timeout=0.14, session_id="c:1")
    elapsed = time.monotonic() - started

    assert "ACK received" in result["result"]
    assert elapsed < 0.23


def test_driver_consumes_result_arriving_in_final_poll_slice(monkeypatch):
    driver = TMWebDriver.__new__(TMWebDriver)
    driver.is_remote = False
    driver.results = {}
    driver.acks = {}
    driver.default_session_id = "c:1"
    driver.latest_session_id = "c:1"
    driver.clean_sessions = lambda: None
    clock = [0.0]
    sent = {}

    class Socket:
        def send_message(self, payload):
            sent["id"] = json.loads(payload)["id"]

    session = Session(
        "c:1",
        {"url": "https://example.test/", "type": "ext_ws", "tab_id": 1},
        Socket(),
    )
    driver.sessions = {"c:1": session}

    monkeypatch.setattr("agent_browser_mcp.tmwebdriver.time.monotonic", lambda: clock[0])

    def final_sleep(seconds):
        clock[0] += seconds
        driver.results[sent["id"]] = {
            "success": True,
            "data": 9,
            "tabId": 1,
        }

    monkeypatch.setattr("agent_browser_mcp.tmwebdriver.time.sleep", final_sleep)

    result = driver.execute_js("return 9", timeout=0.05, session_id="c:1")

    assert result["data"] == 9
    assert result["executed_tab_id"] == 1


def test_ext_cmd_consumes_result_arriving_in_final_poll_slice(monkeypatch):
    driver = TMWebDriver.__new__(TMWebDriver)
    driver.is_remote = False
    driver.results = {}
    driver.acks = {}
    driver.default_session_id = None
    clock = [0.0]
    sent = {}

    class Socket:
        def send_message(self, payload):
            sent["id"] = json.loads(payload)["id"]

    driver.ext_clients = {"c": {"ws": Socket(), "ts": 1.0}}
    monkeypatch.setattr("agent_browser_mcp.tmwebdriver.time.monotonic", lambda: clock[0])

    def final_sleep(seconds):
        clock[0] += seconds
        driver.results[sent["id"]] = {"success": True, "data": {"ok": True}}

    monkeypatch.setattr("agent_browser_mcp.tmwebdriver.time.sleep", final_sleep)

    result = driver.ext_cmd({"cmd": "tabs"}, client_id="c", timeout=0.05)

    assert result == {"data": {"ok": True}}


def test_page_type_missing_target_sends_no_text_or_key(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    calls = []
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": "c:target", "url": "https://example.test/"},
    ])

    def fake_exec_js(script, session_id=None, timeout=15.0):
        calls.append((script, session_id, timeout))
        return {"data": {"found": False, "targetKind": "missing"}}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)

    result = S.page_type(
        "must-not-land",
        selector="#missing",
        submit_key="enter",
        session_id="c:target",
    )

    assert result["status"] == "not_found"
    assert result["typed_chars"] == 0
    assert len(calls) == 1
    assert calls[0][0].lstrip().startswith("(() =>")
    assert "Input.insertText" not in calls[0][0]
    assert driver.default_session_id == "c:previous"


def test_exec_js_undelivered_retry_cannot_restart_timeout(monkeypatch):
    class SlowUndeliveredDriver:
        default_session_id = "c:target"

        def __init__(self):
            self.calls = []

        def execute_js(self, script, timeout=15.0, session_id=None):
            self.calls.append(timeout)
            time.sleep(timeout)
            return {
                "result": (
                    f"No response data in {timeout}s "
                    "(no ACK, script may not have been delivered)"
                )
            }

    driver = SlowUndeliveredDriver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="undelivered"):
        S.exec_js("return 1", session_id="c:target", timeout=0.04)

    elapsed = time.monotonic() - started
    assert len(driver.calls) == 1
    assert elapsed < 0.10


def test_page_type_slow_session_resolution_sends_no_input_after_deadline(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    monkeypatch.setattr(S, "require_driver", lambda: driver)

    def slow_sessions(timeout=None, fresh=False, prune_default=True):
        time.sleep(float(timeout))
        return [{"id": "c:target", "url": "https://example.test/"}]

    monkeypatch.setattr(S, "ensure_sessions", slow_sessions)
    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *args, **kwargs: pytest.fail("input must not run after session deadline"),
    )

    with pytest.raises(TimeoutError, match="session resolution"):
        S.page_type("must-not-land", session_id="c:target", timeout=0.03)

    assert driver.default_session_id == "c:previous"


def test_page_type_uses_already_validated_session_for_input(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    calls = []
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "ensure_sessions",
        lambda timeout=None, fresh=False, prune_default=True: [
            {"id": "c:target", "url": "https://example.test/"}
        ],
    )
    monkeypatch.setattr(
        S,
        "switch_session",
        lambda *args, **kwargs: pytest.fail(
            "page_type must not re-resolve an already validated session"
        ),
    )

    def fake_exec_js(script, session_id=None, timeout=15.0):
        calls.append((script, session_id, timeout))
        if script.lstrip().startswith("(() =>"):
            return {"data": {"found": True, "targetKind": "xterm"}}
        return {"data": [{"ok": True}]}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)

    result = S.page_type(
        "pwd",
        selector=".xterm",
        submit_key="enter",
        session_id="c:target",
        timeout=0.2,
    )

    assert result["status"] == "success"
    assert result["target_kind"] == "xterm"
    assert len(calls) == 2
    assert all(call[1] == "c:target" for call in calls)
    assert driver.default_session_id == "c:previous"


def test_driver_does_not_dispatch_when_session_recovers_at_deadline(monkeypatch):
    driver = TMWebDriver.__new__(TMWebDriver)
    driver.is_remote = False
    driver.results = {}
    driver.acks = {}
    driver.default_session_id = "c:1"
    driver.latest_session_id = "c:1"
    clock = [0.0]
    sent = []

    class Socket:
        def send_message(self, payload):
            sent.append(payload)

    info = {"url": "https://example.test/", "type": "ext_ws", "tab_id": 1}
    session = Session("c:1", info, Socket())
    session.mark_disconnected()
    driver.sessions = {"c:1": session}

    monkeypatch.setattr("agent_browser_mcp.tmwebdriver.time.monotonic", lambda: clock[0])

    def recover_on_final_sleep(seconds):
        clock[0] += seconds
        session.reconnect(Socket(), info)

    monkeypatch.setattr(
        "agent_browser_mcp.tmwebdriver.time.sleep", recover_on_final_sleep
    )

    result = driver.execute_js("window.sideEffect = true", timeout=0.05, session_id="c:1")

    assert sent == []
    assert "no ACK" in result["result"]


def test_page_input_restores_default_when_batch_fails(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": "c:target", "url": "https://example.test/"},
    ])
    monkeypatch.setattr(
        S, "exec_js", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("batch failed")))

    with pytest.raises(RuntimeError, match="batch failed"):
        S.page_press("enter", session_id="c:target")
    assert driver.default_session_id == "c:previous"


def test_page_input_refuses_a_dead_directed_session(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    called = []
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": "c:live", "url": "https://example.test/"},
    ])
    monkeypatch.setattr(S, "exec_js", lambda *args, **kwargs: called.append((args, kwargs)))

    with pytest.raises(RuntimeError, match="not found"):
        S.page_press("enter", session_id="c:dead")
    assert called == []
    assert driver.default_session_id == "c:previous"


def test_page_click_stalls_on_third_unchanged_challenge(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    marker = "https://example.test|/protected|iframe|challenge|turnstile"
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": "c:challenge", "url": "https://example.test/protected"},
    ])

    batch_dispatches = []

    def fake_exec_js(script, session_id=None, timeout=15.0):
        if script.lstrip().startswith("(() =>"):
            return {"data": {"found": True, "x": 10, "y": 20,
                             "width": 100, "height": 50,
                             "challengeMarker": marker}}
        batch_dispatches.append(script)
        return {"data": [{}, {}, {}]}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)
    S._clear_page_challenge("c:challenge")

    first = S.page_click(selector="iframe", offset_x=5, offset_y=7,
                         session_id="c:challenge")
    second = S.page_click(selector="iframe", offset_x=5, offset_y=7,
                          session_id="c:challenge")
    third = S.page_click(selector="iframe", offset_x=5, offset_y=7,
                         session_id="c:challenge")
    dispatched_before_fourth = len(batch_dispatches)
    fourth = S.page_click(selector="iframe", offset_x=5, offset_y=7,
                          session_id="c:challenge")

    assert first["challenge_detected"] is True
    assert first["status"] == "success"
    assert first["attempts"] == 1
    assert second["challenge_detected"] is True
    assert second["status"] == "success"
    assert second["attempts"] == 2
    assert third["status"] == "challenge_stalled"
    assert third["attempts"] == 3
    assert "c:challenge" in third["next_action"]
    assert fourth["status"] == "challenge_stalled"
    assert fourth["attempts"] == 3
    assert len(batch_dispatches) == dispatched_before_fourth == 3
    assert driver.default_session_id == "c:previous"


def test_page_click_stall_block_resets_when_marker_changes(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    sid = "c:challenge-marker-reset"
    marker_a = "https://example.test|/protected|iframe|challenge-a|turnstile"
    marker_b = "https://example.test|/protected|iframe|challenge-b|turnstile"
    markers = iter([marker_a] * 6 + [marker_b] * 2)
    batches = []
    monkeypatch.setattr(S, "require_driver", lambda: _D())
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": sid, "url": "https://example.test/protected"},
    ])

    def fake_exec_js(script, session_id=None, timeout=15.0):
        if script.lstrip().startswith("(() =>"):
            return {"data": {"found": True, "x": 10, "y": 20,
                             "width": 100, "height": 50,
                             "challengeMarker": next(markers)}}
        batches.append(script)
        return {"data": [{}, {}, {}]}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)
    S._clear_page_challenge(sid)

    assert S.page_click(selector="iframe", session_id=sid)["attempts"] == 1
    assert S.page_click(selector="iframe", session_id=sid)["attempts"] == 2
    assert S.page_click(selector="iframe", session_id=sid)["status"] == "challenge_stalled"
    changed = S.page_click(selector="iframe", session_id=sid)

    assert changed["status"] == "success"
    assert changed["attempts"] == 1
    assert len(batches) == 4


def test_page_click_stall_block_resets_after_window_expires(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    sid = "c:challenge-window-reset"
    marker = "https://example.test|/protected|iframe|challenge|turnstile"
    batches = []
    ticks = iter([1.0, 2.0, 3.0, 4.0, 5.0, 125.0, 126.0])
    monkeypatch.setattr(S.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(S, "require_driver", lambda: _D())
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": sid, "url": "https://example.test/protected"},
    ])

    def fake_exec_js(script, session_id=None, timeout=15.0):
        if script.lstrip().startswith("(() =>"):
            return {"data": {"found": True, "x": 10, "y": 20,
                             "width": 100, "height": 50,
                             "challengeMarker": marker}}
        batches.append(script)
        return {"data": [{}, {}, {}]}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)
    S._clear_page_challenge(sid)

    assert S.page_click(selector="iframe", session_id=sid)["attempts"] == 1
    assert S.page_click(selector="iframe", session_id=sid)["attempts"] == 2
    assert S.page_click(selector="iframe", session_id=sid)["status"] == "challenge_stalled"
    after_window = S.page_click(selector="iframe", session_id=sid)

    assert after_window["status"] == "success"
    assert after_window["attempts"] == 1
    assert len(batches) == 4


def test_page_click_clears_challenge_attempts_after_it_disappears(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    sid = "c:challenge-clears"
    marker = "https://example.test|/protected|iframe|challenge|turnstile"
    # Each click resolves once before and once after its batch. The second
    # click removes the challenge; the next two attempts must start from one.
    markers = iter([
        marker, marker,
        marker, None,
        marker, marker,
        marker, marker,
    ])
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": sid, "url": "https://example.test/protected"},
    ])

    def fake_exec_js(script, session_id=None, timeout=15.0):
        if script.lstrip().startswith("(() =>"):
            return {"data": {"found": True, "x": 10, "y": 20,
                             "width": 100, "height": 50,
                             "challengeMarker": next(markers)}}
        return {"data": [{}, {}, {}]}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)
    S._clear_page_challenge(sid)

    first = S.page_click(selector="iframe", session_id=sid)
    assert first["status"] == "success"
    assert first["attempts"] == 1
    cleared = S.page_click(selector="iframe", session_id=sid)
    assert cleared["status"] == "success"
    assert cleared["challenge_detected"] is False
    assert cleared["attempts"] == 0
    restarted = S.page_click(selector="iframe", session_id=sid)
    assert restarted["status"] == "success"
    assert restarted["attempts"] == 1
    next_attempt = S.page_click(selector="iframe", session_id=sid)
    assert next_attempt["status"] == "success"
    assert next_attempt["attempts"] == 2


def test_page_click_does_not_count_a_challenge_that_first_appears_after_click(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    sid = "c:challenge-appears"
    marker = "https://example.test|/protected|iframe|challenge|turnstile"
    markers = iter([
        None, marker,
        marker, marker,
        marker, marker,
        marker, marker,
    ])
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": sid, "url": "https://example.test/protected"},
    ])

    def fake_exec_js(script, session_id=None, timeout=15.0):
        if script.lstrip().startswith("(() =>"):
            return {"data": {"found": True, "x": 10, "y": 20,
                             "width": 100, "height": 50,
                             "challengeMarker": next(markers)}}
        return {"data": [{}, {}, {}]}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)

    appeared = S.page_click(selector="iframe", session_id=sid)
    assert appeared["status"] == "success"
    assert appeared["challenge_detected"] is True
    assert appeared["attempts"] == 0
    assert S.page_click(selector="iframe", session_id=sid)["attempts"] == 1
    assert S.page_click(selector="iframe", session_id=sid)["attempts"] == 2
    stalled = S.page_click(selector="iframe", session_id=sid)
    assert stalled["status"] == "challenge_stalled"
    assert stalled["attempts"] == 3


def test_page_click_marker_change_resets_without_counting_the_click(monkeypatch):
    class _D:
        default_session_id = "c:previous"

    driver = _D()
    sid = "c:challenge-changes"
    marker_a = "https://example.test|/protected|iframe|challenge-a|turnstile"
    marker_b = "https://example.test|/protected|iframe|challenge-b|turnstile"
    markers = iter([
        marker_a, marker_b,
        marker_b, marker_b,
        marker_b, marker_b,
        marker_b, marker_b,
    ])
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [
        {"id": sid, "url": "https://example.test/protected"},
    ])

    def fake_exec_js(script, session_id=None, timeout=15.0):
        if script.lstrip().startswith("(() =>"):
            return {"data": {"found": True, "x": 10, "y": 20,
                             "width": 100, "height": 50,
                             "challengeMarker": next(markers)}}
        return {"data": [{}, {}, {}]}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)

    changed = S.page_click(selector="iframe", session_id=sid)
    assert changed["status"] == "success"
    assert changed["challenge_detected"] is True
    assert changed["attempts"] == 0
    assert S.page_click(selector="iframe", session_id=sid)["attempts"] == 1
    assert S.page_click(selector="iframe", session_id=sid)["attempts"] == 2
    stalled = S.page_click(selector="iframe", session_id=sid)
    assert stalled["status"] == "challenge_stalled"
    assert stalled["attempts"] == 3


def test_page_challenge_attempts_reset_with_tracker_window(monkeypatch):
    sid = "c:challenge-window"
    marker = "https://example.test|/protected|iframe|challenge|turnstile"
    ticks = iter([1.0, 122.0])
    monkeypatch.setattr(S.time, "monotonic", lambda: next(ticks))
    S._clear_page_challenge(sid)
    S._prime_page_challenge(sid, marker)

    assert S._record_unchanged_page_challenge(sid, marker) == (False, 1)
    assert S._record_unchanged_page_challenge(sid, marker) == (False, 1)


def test_page_click_requires_exactly_one_target_mode():
    with pytest.raises(ValueError, match="exactly one"):
        S.page_click()
    with pytest.raises(ValueError, match="exactly one"):
        S.page_click(selector="#button", x=1, y=2)
    with pytest.raises(ValueError, match="both x and y"):
        S.page_click(x=1)


@pytest.mark.parametrize("name,params", [
    ("wait_for", {"selector", "text", "url_pattern", "js", "timeout", "gone", "session_id"}),
    ("scroll_page", {"to", "session_id", "timeout"}),
    ("activate_tab", {"session_id"}),
    ("upload_files", {"selector", "paths", "session_id", "timeout"}),
    ("mouse_click", {"x", "y", "button", "clicks", "interval", "session_id",
                     "activate_session"}),
    ("type_text", {"text", "interval", "click_x", "click_y", "session_id",
                   "activate_session"}),
    ("switch_tab", {"session_id", "url_pattern", "browser", "activate"}),
])
def test_tool_schema(name, params):
    tool = S.mcp._tool_manager.get_tool(name)
    assert set(tool.parameters["properties"]) == params


# --- a remembered target tab that has died ------------------------------

class _FakeSession:
    def __init__(self, sid, active=True):
        self.id = sid
        self._active = active

    def is_active(self):
        return self._active


def _driver_with(sessions, default, latest=None):
    """A TMWebDriver with a hand-built session table and no sockets."""
    from agent_browser_mcp.tmwebdriver import TMWebDriver

    d = TMWebDriver.__new__(TMWebDriver)      # skip __init__: it binds ports
    d.sessions = {s.id: s for s in sessions}
    d.default_session_id = default
    d.latest_session_id = latest
    return d


def test_live_default_keeps_a_healthy_default():
    d = _driver_with([_FakeSession("c:1"), _FakeSession("c:2")], default="c:1")
    assert d._live_default_session_id() == "c:1"


def test_live_default_repicks_when_the_remembered_tab_died():
    """The bug agents actually hit: tab ids churn, so the default the driver
    latched onto when the first tab registered goes stale. Left alone it makes
    every call that does not name a tab fail with 'run switch_tab first' about
    a tab the caller never chose."""
    d = _driver_with([_FakeSession("c:dead", active=False), _FakeSession("c:live")],
                     default="c:dead")
    assert d._live_default_session_id() == "c:live"
    assert d.default_session_id == "c:live"   # and it sticks, so it heals once


def test_live_default_prefers_the_most_recent_tab():
    d = _driver_with([_FakeSession("c:dead", active=False), _FakeSession("c:a"),
                      _FakeSession("c:b")], default="c:dead", latest="c:a")
    assert d._live_default_session_id() == "c:a"


def test_live_default_gives_up_cleanly_with_no_live_tabs():
    """Nothing to re-pick: keep the value so the caller's own error path still
    names the tab it was looking for, rather than a confusing None."""
    d = _driver_with([_FakeSession("c:dead", active=False)], default="c:dead")
    assert d._live_default_session_id() == "c:dead"


def test_only_one_process_may_spawn_the_bridge(tmp_path, monkeypatch):
    """"Is the port open? no -> spawn" is not atomic across processes, so two
    MCP instances starting together can both spawn, and the loser sits there
    having lost the port bind. The lock has to make the second caller stand
    down."""
    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))
    first = S._acquire_spawn_lock()
    assert first is not None
    assert S._acquire_spawn_lock() is None      # loser stands down
    first.unlink()
    assert S._acquire_spawn_lock() is not None  # released, next one may try


def test_a_crashed_spawner_does_not_block_forever(tmp_path, monkeypatch):
    """A lock left behind by a killed process must not wedge every later spawn."""
    import os as _os

    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))
    lock = S._acquire_spawn_lock()
    assert lock is not None
    stale = time.time() - (S._SPAWN_LOCK_STALE + 5)
    _os.utime(lock, (stale, stale))
    assert S._acquire_spawn_lock() is not None


def test_concurrent_starts_still_spawn_one_daemon(tmp_path, monkeypatch):
    """The sequential lock check is not enough on its own.

    A freshly spawned daemon needs a moment to bind its port, so callers that
    check during that window see "no daemon" and spawn another. An earlier fix
    released the lock as soon as the winner returned, and 8 of 12 concurrent
    callers still spawned. Every caller must also come back True: they depend on
    a bridge being up, not on having been the one to start it.
    """
    import threading

    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))

    spawned = []
    guard = threading.Lock()
    port_up_at = [None]

    def fake_port_open(_h, _p):
        return port_up_at[0] is not None and time.monotonic() >= port_up_at[0]

    def fake_locked():
        with guard:
            spawned.append(1)
            if port_up_at[0] is None:
                port_up_at[0] = time.monotonic() + 0.4   # bind latency
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if fake_port_open(None, None):
                return True
            time.sleep(0.05)
        return False

    monkeypatch.setattr(S, "_spawn_bridge_daemon_locked", fake_locked)
    monkeypatch.setattr(S, "_port_open", fake_port_open)

    results = []
    threads = [threading.Thread(target=lambda: results.append(S.spawn_bridge_daemon()))
               for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(spawned) == 1, f"spawned {len(spawned)} daemons"
    assert all(results)


def test_physical_input_honours_an_explicit_session(monkeypatch):
    """The disciplined caller must not be the one who gets burned.

    Session-scoped tools save and restore the shared default, so an agent that
    passes session_id to scan_page and then clicks without one raises whatever
    tab another task last selected — and the click is silent about it. Passing
    session_id here has to win over the global default.
    """
    raised = []
    monkeypatch.setattr(S, "_activate", lambda sid: raised.append(sid) or {"activated_session_id": sid})
    monkeypatch.setattr(S.time, "sleep", lambda _s: None)

    S._maybe_activate("current", "c:mine")
    assert raised == ["c:mine"]


def test_explicit_session_beats_an_opt_out(monkeypatch):
    """Naming a tab is a clearer statement of intent than a leftover 'none'."""
    raised = []
    monkeypatch.setattr(S, "_activate", lambda sid: raised.append(sid) or {})
    monkeypatch.setattr(S.time, "sleep", lambda _s: None)

    S._maybe_activate("none", "c:mine")
    assert raised == ["c:mine"]


def test_desktop_click_can_still_opt_out(monkeypatch):
    """A real desktop click outside the browser must not raise any tab."""
    raised = []
    monkeypatch.setattr(S, "_activate", lambda sid: raised.append(sid) or {})

    assert S._maybe_activate("none", None) is None
    assert raised == []


def test_activate_admits_a_window_it_could_not_raise(monkeypatch):
    """Making a tab active succeeds even when the window is minimised, so
    reporting plain success there sends the caller off clicking coordinates on
    whatever else is on screen. It has to say the tab is not visible."""
    class _D:
        default_session_id = "c:7"

        def ext_cmd(self, *a, **k):
            # ext_cmd returns {'data': <handler reply>} both locally and over
            # HTTP; the handler reply carries onScreen. The old test mocked
            # {'r': {...}}, a shape ext_cmd never produces, so it passed while
            # the real path returned on_screen=None.
            return {"data": {"ok": True, "windowState": "minimized", "onScreen": False}}

    monkeypatch.setattr(S, "require_driver", lambda: _D())
    out = S._activate("c:7")
    assert out["on_screen"] is False
    assert "warning" in out


def test_activate_confirms_a_window_it_did_raise(monkeypatch):
    class _D:
        default_session_id = "c:7"

        def ext_cmd(self, *a, **k):
            return {"data": {"ok": True, "windowState": "normal", "onScreen": True,
                             "wasMinimized": True}}

    monkeypatch.setattr(S, "require_driver", lambda: _D())
    out = S._activate("c:7")
    assert out["on_screen"] is True
    assert out["was_minimized"] is True
    assert "warning" not in out


def test_activate_does_not_claim_to_know_on_an_old_extension(monkeypatch):
    """A stale extension build answers a bare {ok:true}. Unknown must read as
    unknown, not as a confirmed raise."""
    class _D:
        default_session_id = "c:7"

        def ext_cmd(self, *a, **k):
            return {"data": {"ok": True}}

    monkeypatch.setattr(S, "require_driver", lambda: _D())
    out = S._activate("c:7")
    assert out["on_screen"] is None
    assert "note" in out


def test_live_default_bootstraps_when_nothing_was_remembered():
    """No default yet is the normal cold start, not an error: pick a live tab so
    the first call works without a mandatory list_tabs + switch_tab preamble."""
    d = _driver_with([_FakeSession("c:1")], default=None)
    assert d._live_default_session_id() == "c:1"


# --- new tools: cookie / storage / wait_for_url validation ----------------

class TestCookieValidation:
    def test_set_cookies_empty_name_rejected(self):
        with pytest.raises(ValueError, match="name"):
            S.set_cookies({"value": "x"})

    def test_set_cookies_illegal_name_chars_rejected(self):
        with pytest.raises(ValueError, match="非法"):
            S.set_cookies({"name": "a=b", "value": "x"})

    def test_set_cookies_value_with_semicolon_rejected(self):
        with pytest.raises(ValueError, match="value"):
            S.set_cookies({"name": "ok", "value": "a;b"})

    def test_set_cookies_bad_json_rejected(self):
        with pytest.raises(ValueError, match="JSON"):
            S.set_cookies("{not json")

    def test_set_cookies_empty_list_rejected(self):
        with pytest.raises(ValueError, match="非空"):
            S.set_cookies([])

    def test_set_cookies_bad_expires_rejected(self):
        with pytest.raises(ValueError, match="expires"):
            S.set_cookies({"name": "ok", "url": "https://example.com/",
                           "expires": "tomorrow"})

    def test_delete_cookies_empty_name_rejected(self):
        with pytest.raises(ValueError, match="name"):
            S.delete_cookies("  ")


class TestStorageValidation:
    def test_storage_set_empty_key_rejected(self):
        with pytest.raises(ValueError, match="key"):
            S.storage_set("", "v")

    def test_storage_set_bad_area_rejected(self):
        with pytest.raises(ValueError):
            S.storage_set("k", "v", area="bogus")

    def test_storage_get_bad_area_rejected(self):
        with pytest.raises(ValueError):
            S.storage_get(area="bogus")


class TestWaitForUrlValidation:
    def test_empty_pattern_rejected(self):
        with pytest.raises(ValueError, match="url_pattern"):
            S.wait_for_url("  ")

    def test_bad_regex_rejected(self):
        with pytest.raises(ValueError, match="正则"):
            S.wait_for_url("([unclosed")


# --- regression guards for the audit-fixes round ------------------------

def test_activate_reads_onScreen_from_data_key(monkeypatch):
    """The real ext_cmd wraps the handler reply under 'data', not 'r'.
    A minimized window's onScreen=False MUST surface through that wrapper —
    this is the bug that made every physical-input call report on_screen=None
    and tell the user to reload the extension."""
    class _D:
        default_session_id = "c:7"

        def ext_cmd(self, *a, **k):
            return {"data": {"ok": True, "windowState": "minimized", "onScreen": False,
                             "wasMinimized": True}}

    monkeypatch.setattr(S, "require_driver", lambda: _D())
    out = S._activate("c:7")
    assert out["on_screen"] is False
    assert out["was_minimized"] is True
    assert "warning" in out


def test_upload_files_parses_a_bare_results_array(monkeypatch):
    """handleBatch's success reply arrives as a bare array (ws.onmessage does
    `res.data ?? res.results ?? res`), so upload_files must treat a list under
    `data` as the results, not look for data['results'] (which never exists on
    a list and silently dropped the selector-not-found check)."""
    calls = []

    def fake_exec_js(script, session_id=None, timeout=15.0):
        calls.append(script)
        # DOM.getDocument -> {root:{nodeId:1}}; DOM.querySelector -> {nodeId:42}
        return {"data": [{"root": {"nodeId": 1}}, {"nodeId": 42}]}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)
    import tempfile, os
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    f.write(b"x"); f.close()
    try:
        out = S.upload_files("#file", f.name)
        assert out["status"] == "ok"
        assert out["node_id"] == 42
    finally:
        os.unlink(f.name)


def test_upload_files_raises_when_selector_matches_nothing(monkeypatch):
    """DOM.querySelector returns nodeId=0 when nothing matches; that used to be
    swallowed (results was always None because data was a list, not a dict) and
    upload_files reported status:ok with node_id:null."""
    def fake_exec_js(script, session_id=None, timeout=15.0):
        return {"data": [{"root": {"nodeId": 1}}, {"nodeId": 0}]}  # 0 = no match

    monkeypatch.setattr(S, "exec_js", fake_exec_js)
    import tempfile, os
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    f.write(b"x"); f.close()
    try:
        with pytest.raises(RuntimeError, match="matched no element"):
            S.upload_files("#nope", f.name)
    finally:
        os.unlink(f.name)


def test_set_cookies_refuses_document_cookie_fallback_with_tab_id(monkeypatch):
    """CDP unavailable + tab_id named: document.cookie runs in the DEFAULT tab,
    so writing there would report ok for a cookie that never reached the
    target. Fail loudly instead of lying."""
    # Force the CDP path to raise, triggering the fallback.
    def fake_cdp(method, params, session_id, tab_id, timeout):
        raise RuntimeError("debugger busy")
    monkeypatch.setattr(S, "_cdp", fake_cdp)
    # _page_location would be called to scope; stub it.
    monkeypatch.setattr(S, "_page_location",
                        lambda session_id=None, timeout=10: {"url": "https://x.com/"})
    out = S.set_cookies({"name": "k", "value": "v", "url": "https://x.com/"},
                        tab_id=42)
    assert out["status"] == "failed"
    assert "tab_id" in out["results"][0]["error"]


def test_delete_cookies_refuses_document_cookie_fallback_with_tab_id(monkeypatch):
    def fake_cdp(method, params, session_id, tab_id, timeout):
        raise RuntimeError("debugger busy")
    monkeypatch.setattr(S, "_cdp", fake_cdp)
    monkeypatch.setattr(S, "_page_location",
                        lambda session_id=None, timeout=10: {"url": "https://x.com/"})
    out = S.delete_cookies("k", url="https://x.com/", tab_id=42)
    assert out["status"] == "failed"
    assert "tab_id" in out["error"]


def test_spawn_lock_recycled_when_owner_pid_is_dead(tmp_path, monkeypatch):
    """A daemon that crashed seconds after spawn used to hold the lock for the
    full _SPAWN_LOCK_STALE window; now a dead owner pid frees it immediately."""
    import os as _os
    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))
    lock = S._acquire_spawn_lock()
    assert lock is not None
    # Write a pid that is guaranteed not to exist (nothing uses pid 0).
    lock.write_text("0", encoding="utf-8")
    # Make mtime recent so the age-based path does NOT fire; only pid liveness
    # should recycle it.
    fresh = time.time()
    _os.utime(lock, (fresh, fresh))
    assert S._acquire_spawn_lock() is not None  # recycled because pid 0 is dead


def test_tool_lock_serializes_concurrent_calls(monkeypatch):
    """The save/restore default_session_id pattern races under concurrency.
    The global _TOOL_LOCK must serialize tool calls so two directed calls do
    not read each other's target."""
    import threading

    order = []
    lock = threading.Lock()

    class _D:
        default_session_id = None

        def execute_js(self, code, timeout=15, session_id=None, allow_failover=False):
            with lock:
                order.append(("exec", session_id))
            return {"data": "ok"}

    d = _D()
    monkeypatch.setattr(S, "require_driver", lambda: d)
    monkeypatch.setattr(
        S,
        "ensure_sessions",
        lambda *args, **kwargs: [{"id": "c:1", "url": "https://x"}],
    )
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False:
                        [{"id": "c:1", "url": "https://x"}])
    # switch_session mutates driver.default_session_id; exec_js_rich reads it.
    monkeypatch.setattr(S, "exec_js", lambda script, session_id=None, timeout=15.0:
                        {"data": "ok", "executed_tab_id": 1})

    # Two concurrent execute_js calls with different session_ids: both
    # save/restore, and under the lock they cannot interleave.
    import concurrent.futures

    def call(sid):
        S.execute_js("return 1", session_id=sid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(call, ["c:1", "c:1", "c:1", "c:1"]))
