"""Live tests: need the bridge daemon running and the extension connected.

Skipped unless you ask for them:  pytest -m live

Everything runs in ONE scratch tab (the scratch_session fixture) so the user's
own tabs are never touched and no tab is left behind. Sites are picked for
different rendering shapes: static HTML, docs with many relative links, a
server-rendered list, and a JS-heavy app.
"""
import base64
import json
import re

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from agent_browser_mcp import server as S

pytestmark = pytest.mark.live

STATIC = "https://example.com/"
DOCS = "https://developer.mozilla.org/en-US/docs/Web/API/Window/scrollY"
LIST = "https://news.ycombinator.com/"


def goto(sid, url, selector, timeout=30):
    """Navigate and wait for the page to actually have content."""
    nav = S.open_url(url, session_id=sid, timeout=timeout)
    got = S.wait_for(selector=selector, timeout=timeout, session_id=sid)
    assert got["status"] == "success", f"{url} never rendered {selector}: {got}"
    return nav


class TestScreenshotContent:
    def test_real_capture_returns_mcp_image_content(self, scratch_session):
        goto(scratch_session, STATIC, "h1")

        result = S.capture_page_screenshot(session_id=scratch_session)

        assert isinstance(result, CallToolResult)
        assert isinstance(result.content[0], TextContent)
        assert isinstance(result.content[1], ImageContent)
        assert result.content[1].mimeType == "image/png"
        assert base64.b64decode(result.content[1].data).startswith(b"\x89PNG\r\n\x1a\n")
        assert result.structuredContent["image_attached"] is True


class TestOpenUrl:
    def test_reports_where_it_landed(self, scratch_session):
        nav = goto(scratch_session, STATIC, "h1")
        assert nav["requested_url"] == STATIC
        assert nav["url"]
        assert nav["status"] in ("ok", "redirected")

    def test_landing_url_is_read_back_not_echoed(self, scratch_session):
        """The old code returned the requested URL, so a redirect to a login
        page still came back as status:"ok" with the URL you asked for."""
        nav = goto(scratch_session, STATIC, "h1")
        assert nav.get("title"), "no title read back from the landed page"


class TestDialogPolicy:
    @staticmethod
    def _all_tabs(sid):
        reply = S.list_all_tabs(session_id=sid)
        return reply.get("data") or reply.get("result", {}).get("data", [])

    def test_injected_alert_confirm_and_prompt_are_observable(self, scratch_session):
        goto(scratch_session, STATIC, "h1")

        alert = S.execute_js(
            "alert('ABM alert'); return 'continued'",
            session_id=scratch_session,
            dialog_policy="dismiss",
            no_monitor=True,
        )
        assert alert["status"] == "ok"
        assert alert["js_return"] == "continued"
        assert alert["dialog"]["type"] == "alert"
        assert alert["dialog"]["message"] == "ABM alert"

        dismissed = S.execute_js(
            "return confirm('ABM confirm')",
            session_id=scratch_session,
            dialog_policy="dismiss",
            no_monitor=True,
        )
        assert dismissed["status"] == "ok"
        assert dismissed["js_return"] is False
        assert dismissed["dialog"]["type"] == "confirm"

        accepted = S.execute_js(
            "return confirm('ABM confirm')",
            session_id=scratch_session,
            dialog_policy="accept",
            no_monitor=True,
        )
        assert accepted["status"] == "ok"
        assert accepted["js_return"] is True

        prompt = S.execute_js(
            "return prompt('ABM prompt', 'typed text')",
            session_id=scratch_session,
            dialog_policy="accept",
            no_monitor=True,
        )
        assert prompt["status"] == "ok"
        assert prompt["js_return"] == "typed text"
        assert prompt["dialog"]["defaultPrompt"] == "typed text"

    def test_beforeunload_dismiss_then_accept(self, scratch_session):
        original_active = next(
            (tab for tab in self._all_tabs(scratch_session) if tab.get("active")), None
        )
        client_id = str(scratch_session).rsplit(":", 1)[0]
        try:
            goto(scratch_session, STATIC, "h1")
            S.execute_js(
                """
                document.body.insertAdjacentHTML(
                  'beforeend', '<button id="abm-arm-beforeunload">Arm</button>');
                window.__abmBeforeUnload = event => {
                  event.preventDefault();
                  event.returnValue = '';
                };
                document.querySelector('#abm-arm-beforeunload').onclick = () => {
                  addEventListener('beforeunload', window.__abmBeforeUnload);
                };
                return true;
                """,
                session_id=scratch_session,
                no_monitor=True,
            )
            S.page_click(selector="#abm-arm-beforeunload", session_id=scratch_session)

            dismissed = S.open_url(
                "https://example.org/",
                session_id=scratch_session,
                beforeunload="dismiss",
                timeout=20,
            )
            assert dismissed["status"] == "blocked_by_beforeunload"
            assert "example.com" in dismissed["url"]
            assert dismissed["dialog"]["type"] == "beforeunload"

            accepted = S.open_url(
                "https://example.org/",
                session_id=scratch_session,
                beforeunload="accept",
                timeout=20,
            )
            assert accepted["status"] == "ok"
            S.wait_for(url_pattern="example.org", session_id=scratch_session, timeout=20)
        finally:
            try:
                S.execute_js(
                    "removeEventListener('beforeunload', window.__abmBeforeUnload); return true",
                    session_id=scratch_session,
                    no_monitor=True,
                )
            except Exception:
                pass
            if original_active is not None:
                S.require_driver().ext_cmd(
                    {"cmd": "tabs", "method": "switch", "tabId": original_active["id"]},
                    client_id=client_id,
                    timeout=15.0,
                )


class TestWaitFor:
    def test_selector_present(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(selector="h1", timeout=10, session_id=scratch_session)
        assert r["status"] == "success"
        assert r["waited_ms"] is not None

    def test_text_present(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(text="Example Domain", timeout=10, session_id=scratch_session)
        assert r["status"] == "success"

    def test_url_pattern(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(url_pattern=r"example\.com", timeout=10,
                       session_id=scratch_session)
        assert r["status"] == "success"

    def test_js_expression(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(js="document.readyState === 'complete'", timeout=10,
                       session_id=scratch_session)
        assert r["status"] == "success"

    def test_absent_selector_times_out(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(selector="#definitely-not-here-xyz", timeout=3,
                       session_id=scratch_session)
        assert r["status"] == "timeout"
        # It must actually have waited, not returned instantly.
        assert r["waited_ms"] >= 2500, r["waited_ms"]

    def test_gone_on_a_permanent_element_times_out(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(selector="body", gone=True, timeout=3,
                       session_id=scratch_session)
        assert r["status"] == "timeout"

    def test_a_long_wait_is_still_one_roundtrip(self, scratch_session):
        """Polling happens in-page, so a 5s wait must not cost 50 bridge calls.
        Measured indirectly: the call returns close to the timeout, not much
        later, which it would if each poll were a roundtrip."""
        import time

        goto(scratch_session, STATIC, "h1")
        t0 = time.time()
        r = S.wait_for(selector="#nope-xyz", timeout=4, session_id=scratch_session)
        elapsed = time.time() - t0
        assert r["status"] == "timeout"
        assert elapsed < 12, f"took {elapsed:.1f}s for a 4s in-page wait"


class TestScrollPage:
    def test_bottom_then_top(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        bottom = S.scroll_page(to="bottom", session_id=scratch_session, timeout=20)
        assert isinstance(bottom["scroll_y"], int)
        assert bottom["doc_height"] > 0
        if bottom["doc_height"] > bottom["viewport_height"] + 100:
            assert bottom["moved"] or bottom["at_bottom"]
        top = S.scroll_page(to="top", session_id=scratch_session, timeout=20)
        assert top["scroll_y"] == 0

    def test_pixel_offset(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        S.scroll_page(to="top", session_id=scratch_session, timeout=20)
        r = S.scroll_page(to="300", session_id=scratch_session, timeout=20)
        assert r["scroll_y"] > 0

    def test_selector_scrolls_into_view(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        r = S.scroll_page(to="footer, h1", session_id=scratch_session, timeout=20)
        assert r["status"] == "success"

    def test_missing_selector_does_not_raise(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.scroll_page(to="#not-a-real-element", session_id=scratch_session,
                          timeout=20)
        assert r["status"] == "not_found"
        assert r["selector"] == "#not-a-real-element"
        assert "无匹配" in r["note"]


class TestScanPageLinks:
    def test_refs_replace_long_hrefs(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        assert scan["status"] == "success"
        assert "__link__" not in scan["content"]

    def test_every_ref_resolves(self, scratch_session):
        goto(scratch_session, LIST, "td.title, .titleline")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        assert scan["status"] == "success"
        used = set(re.findall(r'href="#(r\d+)"', scan["content"]))
        assert used <= set(scan.get("links", {})), "unresolvable refs in content"

    def test_refs_are_absolute_urls(self, scratch_session):
        """MDN serves relative hrefs; a ref of '/en-US/docs/x' is useless to
        open_url, so they must be resolved against the page URL."""
        goto(scratch_session, DOCS, "h1")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        links = scan.get("links", {})
        assert links, "docs page produced no link refs"
        relative = [u for u in links.values() if not u.startswith(("http://", "https://"))]
        assert not relative, f"relative refs survived: {relative[:5]}"

    def test_a_ref_can_actually_be_navigated(self, scratch_session):
        """End to end: read a link off the page, then go there."""
        goto(scratch_session, DOCS, "h1")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        target = next((u for u in scan.get("links", {}).values()
                       if u.startswith("https://developer.mozilla.org")), None)
        if not target:
            pytest.skip("no mozilla link to follow on this render")
        nav = S.open_url(target, session_id=scratch_session, timeout=30)
        assert nav["status"] in ("ok", "redirected")
        assert nav["url"]


class TestScanPageOffscreen:
    def test_long_page_reports_dropped_elements(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        S.scroll_page(to="top", session_id=scratch_session, timeout=20)
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        assert scan["status"] == "success"
        off = scan.get("offscreen")
        if off is None:
            pytest.skip("page fit inside the clamp on this viewport")
        assert off["elements"] > 0
        assert off["doc_height"] >= off["viewport_height"]
        assert scan.get("hint"), "offscreen elements but no hint for the agent"

    def test_marker_is_not_left_in_the_content(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        # The base-url marker is an internal detail and must not reach the agent.
        assert "tmwd-base:" not in scan["content"]


class TestNavigationOutcome:
    def test_click_that_navigates_is_not_reported_as_success(self, scratch_session):
        """The core regression: a click that unloads the page used to come back
        as status:"success" with a diagnostic string as the return value."""
        goto(scratch_session, STATIC, "h1")
        rr = S.execute_js(
            "var a=document.createElement('a');"
            "a.href='https://example.org/';document.body.appendChild(a);"
            "a.click();return 'never-seen'",
            session_id=scratch_session, no_monitor=True, timeout=15)
        if rr["status"] == "navigated":
            assert rr.get("js_return") is None
            assert rr.get("js_return_lost")
        else:
            # Fast pages can return before unload; that is a genuine success,
            # but the return value must be the script's, never a bridge string.
            assert rr["status"] == "success"
            assert "reloaded" not in str(rr.get("js_return"))

    def test_plain_return_still_works(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        rr = S.execute_js("return 1+1", session_id=scratch_session,
                          no_monitor=True, timeout=15)
        assert rr["status"] == "success"
        assert rr["js_return"] == 2


class TestExplicitSessionPipeline:
    def test_monitored_execute_stays_on_explicit_session(self, driver, scratch_session):
        """Every monitor/readback roundtrip must remain pinned to the named tab."""
        client_id = str(scratch_session).rsplit(":", 1)[0]
        other_sessions = [
            tab for tab in S.compact_tabs(fresh=True)
            if tab["id"] != scratch_session
            and str(tab["id"]).rsplit(":", 1)[0] == client_id
        ]
        if not other_sessions:
            pytest.skip("need another connected tab in the scratch tab's browser")
        other_session = other_sessions[0]["id"]
        previous_default = driver.default_session_id
        marker = "abm-explicit-session-211"
        try:
            goto(scratch_session, STATIC, "h1")
            driver.default_session_id = other_session
            result = S.execute_js(
                f"document.body.dataset.abmExplicitTarget = '{marker}'; return document.title",
                session_id=scratch_session,
                no_monitor=False,
                timeout=15,
            )
            assert result["status"] in ("ok", "success")
            assert str(result["tab_id"]) == str(scratch_session).rsplit(":", 1)[-1]
            assert driver.default_session_id == other_session

            scratch_marker = S.execute_js(
                "return document.body.dataset.abmExplicitTarget || null",
                session_id=scratch_session,
                no_monitor=True,
                timeout=10,
            )
            other_marker = S.execute_js(
                "return document.body.dataset.abmExplicitTarget || null",
                session_id=other_session,
                no_monitor=True,
                timeout=10,
            )
            assert scratch_marker["js_return"] == marker
            assert other_marker["js_return"] is None
            assert driver.default_session_id == other_session
        finally:
            driver.default_session_id = previous_default


class TestNewTabGeneration:
    def test_open_new_tab_returns_matching_live_generation(self, scratch_session):
        created = S.open_new_tab(
            "https://example.com/?abm-generation=211",
            timeout=20,
            active=False,
            session_id=scratch_session,
        )
        created_sid = created.get("session_id")
        try:
            assert created["status"] == "ok"
            assert created["ready"] is True
            assert created["generation"]
            assert created_sid
            matching = next(
                tab for tab in S.compact_tabs(fresh=True)
                if tab["id"] == created_sid
            )
            assert str(matching["generation"]) == str(created["generation"])
            immediate = S.execute_js(
                "return location.href",
                session_id=created_sid,
                no_monitor=True,
                timeout=15,
            )
            assert immediate["status"] in ("ok", "success")
            assert "abm-generation=211" in immediate["js_return"]
        finally:
            if created_sid:
                S.close_tabs(created_sid)


class TestBackgroundPageInput:
    def test_events_reach_scratch_without_raising_it(self, scratch_session):
        def all_tabs():
            reply = S.list_all_tabs(session_id=scratch_session)
            return reply.get("data") or reply.get("result", {}).get("data", [])

        def is_active(tab_id):
            return next((tab.get("active") for tab in all_tabs()
                         if tab.get("id") == tab_id), None)

        def reset_events():
            S.execute_js(
                "window.__pageInputEvents = []; return true",
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )

        def observed_events():
            observed = S.execute_js(
                "return JSON.stringify(window.__pageInputEvents)",
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )
            return json.loads(observed["js_return"])

        def assert_background_result(result, foreground_tab_id):
            assert result["input_mode"] == "cdp"
            assert result["foreground_changed"] is False
            assert is_active(foreground_tab_id) is True

        original_active = next((tab for tab in all_tabs() if tab.get("active")), None)
        client_id = str(scratch_session).rsplit(":", 1)[0]
        other_sessions = [
            tab for tab in S.compact_tabs(fresh=True)
            if tab["id"] != scratch_session
            and str(tab["id"]).rsplit(":", 1)[0] == client_id
        ]
        if not other_sessions:
            pytest.skip("need another connected tab in the scratch tab's browser")
        foreground_session = other_sessions[0]["id"]
        foreground_tab_id = int(str(foreground_session).rsplit(":", 1)[-1])

        try:
            goto(scratch_session, STATIC, "h1")
            setup = S.execute_js(
                """
                document.body.innerHTML = `
                  <form id="page-input-form">
                    <input id="page-input-field" value="old">
                    <button id="page-input-button" type="button">Click</button>
                  </form>
                  <div class="xterm" id="page-input-xterm">
                    <textarea id="page-input-xterm-helper" class="xterm-helper-textarea"
                              aria-label="Terminal input"></textarea>
                  </div>
                  <div id="page-input-drag" style="width:240px;height:100px"></div>`;
                window.__pageInputEvents = [];
                for (const type of ['click', 'input', 'keydown', 'keyup',
                                    'mousedown', 'mousemove', 'mouseup']) {
                  document.addEventListener(type, event => {
                    window.__pageInputEvents.push({
                      type,
                      id: event.target && event.target.id || '',
                      key: event.key || '',
                      ctrlKey: Boolean(event.ctrlKey),
                      altKey: Boolean(event.altKey),
                      shiftKey: Boolean(event.shiftKey),
                      metaKey: Boolean(event.metaKey),
                      clientX: Number.isFinite(event.clientX) ? event.clientX : null,
                      clientY: Number.isFinite(event.clientY) ? event.clientY : null,
                      button: Number.isFinite(event.button) ? event.button : null,
                      buttons: Number.isFinite(event.buttons) ? event.buttons : null
                    });
                  }, true);
                }
                document.querySelector('#page-input-form').addEventListener('submit', event => {
                  event.preventDefault();
                  window.__pageInputEvents.push({type: 'submit', id: event.target.id, key: ''});
                });
                const rect = document.querySelector('#page-input-drag').getBoundingClientRect();
                return JSON.stringify({
                  x1: rect.left + 10, y1: rect.top + 20,
                  x2: rect.right - 10, y2: rect.top + 20
                });
                """,
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )
            drag = json.loads(setup["js_return"])

            S.activate_tab(session_id=foreground_session)
            assert is_active(foreground_tab_id) is True

            reset_events()
            click = S.page_click(selector="#page-input-button",
                                 session_id=scratch_session)
            assert_background_result(click, foreground_tab_id)
            click_events = observed_events()
            assert any(event["type"] == "click" and event["id"] == "page-input-button"
                       for event in click_events)

            reset_events()
            typed = S.page_type("hello", selector="#page-input-field", clear=True,
                                session_id=scratch_session)
            assert_background_result(typed, foreground_tab_id)
            type_events = observed_events()
            assert any(event["type"] == "input" and event["id"] == "page-input-field"
                       for event in type_events)
            typed_value = S.execute_js(
                "return document.querySelector('#page-input-field').value",
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )
            assert typed_value["js_return"] == "hello"

            reset_events()
            xterm_typed = S.page_type(
                "printf 'abm-xterm'",
                selector="#page-input-xterm",
                submit_key="enter",
                session_id=scratch_session,
            )
            assert_background_result(xterm_typed, foreground_tab_id)
            xterm_state = S.execute_js(
                "return JSON.stringify({value: document.querySelector('.xterm-helper-textarea').value, "
                "active: document.activeElement === document.querySelector('.xterm-helper-textarea')})",
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )
            xterm_state = json.loads(xterm_state["js_return"])
            assert xterm_state == {"value": "printf 'abm-xterm'", "active": True}
            xterm_events = observed_events()
            assert any(event["type"] == "input" and event["id"] == "page-input-xterm-helper"
                       for event in xterm_events)
            assert any(event["type"] == "keydown" and event["key"] == "Enter"
                       for event in xterm_events)

            reset_events()
            pressed = S.page_press("ctrl,a", session_id=scratch_session)
            assert_background_result(pressed, foreground_tab_id)
            press_events = observed_events()
            key_events = [event for event in press_events
                          if event["type"] in ("keydown", "keyup")]
            assert any(event["type"] == "keydown" and event["key"].lower() == "a"
                       and event["ctrlKey"] is True for event in key_events)
            assert any(event["type"] == "keyup" and event["key"].lower() == "a"
                       and event["ctrlKey"] is True for event in key_events)
            assert any(event["key"] == "Control" for event in key_events)

            reset_events()
            dragged = S.page_drag(drag["x1"], drag["y1"], drag["x2"], drag["y2"],
                                  session_id=scratch_session)
            assert_background_result(dragged, foreground_tab_id)
            drag_events = [event for event in observed_events()
                           if event["id"] == "page-input-drag"]
            assert any(event["type"] == "mousedown" for event in drag_events)
            assert any(event["type"] == "mouseup" for event in drag_events)
            move_points = {
                (event["clientX"], event["clientY"])
                for event in drag_events
                if event["type"] == "mousemove"
            }
            assert len(move_points) >= 2
        finally:
            if original_active is not None:
                S.require_driver().ext_cmd(
                    {"cmd": "tabs", "method": "switch", "tabId": original_active["id"]},
                    client_id=client_id,
                    timeout=15.0,
                )


class TestFailoverRefusal:
    def test_dead_session_is_refused_not_redirected(self, driver):
        """A script aimed at a dead tab must not run on a different live tab:
        'click checkout' landing on the wrong page is worse than an error."""
        with pytest.raises(ValueError) as exc:
            driver.execute_js("return 1", timeout=5,
                              session_id="chrome_nonexistent:999999")
        msg = str(exc.value)
        assert "未连接" in msg
        # And it should tell the caller what it could have used instead.
        if driver.get_all_sessions():
            assert "switch_tab" in msg or "活动会话" in msg

    def test_reads_do_not_silently_switch_tabs_either(self, driver):
        """Reading is side-effect free, but silently returning a DIFFERENT page
        than the caller asked for is its own wrong answer, so get_main_block
        defaults to no failover too."""
        from agent_browser_mcp import simphtml

        prev = driver.default_session_id
        try:
            driver.default_session_id = "chrome_nonexistent:999999"
            with pytest.raises(Exception) as exc:
                simphtml.get_main_block(driver, timeout=10)
            assert "未连接" in str(exc.value)
        finally:
            driver.default_session_id = prev

    def test_failover_is_available_when_explicitly_asked_for(self, driver, scratch_session):
        """The escape hatch still works for a caller that genuinely wants any
        live tab rather than a specific one."""
        from agent_browser_mcp import simphtml

        prev = driver.default_session_id
        try:
            driver.default_session_id = "chrome_nonexistent:999999"
            block = simphtml.get_main_block(driver, timeout=25, allow_failover=True)
            assert isinstance(block, str) and len(block) > 50
        finally:
            driver.default_session_id = prev


class TestActivateTab:
    def test_activate_reports_the_tab(self, scratch_session):
        r = S.activate_tab(session_id=scratch_session)
        assert r["status"] == "ok"
        assert r["activated_session_id"] == scratch_session
        assert isinstance(r["tab_id"], int)

    @staticmethod
    def _is_active(session_id):
        """Whether the tab is the active one in its window.

        Deliberately not document.visibilityState: that also reads `hidden`
        when the Chrome window is merely minimised, which a test cannot
        control. `active` is what activation actually promises.
        """
        tab_id = int(str(session_id).rsplit(":", 1)[-1])
        r = S.list_all_tabs()
        for t in r.get("data") or r.get("result", {}).get("data", []):
            if t["id"] == tab_id:
                return t["active"]
        return None

    def test_activate_really_raises_the_tab(self, driver, scratch_session):
        """The report is not the proof — check Chrome agrees."""
        S.activate_tab(session_id=scratch_session)
        assert self._is_active(scratch_session) is True

    def test_switch_tab_is_background_by_default(self, driver, scratch_session):
        """Re-targeting a tab must not steal the browser foreground."""
        S.activate_tab(session_id=scratch_session)
        others = [t for t in S.compact_tabs(fresh=True) if t["id"] != scratch_session]
        if not others:
            pytest.skip("need a second tab to prove the switch moved anything")
        S.switch_tab(session_id=others[0]["id"])
        assert self._is_active(scratch_session) is True

        r = S.switch_tab(session_id=others[0]["id"], activate=True)
        assert r["active_session_id"] == others[0]["id"]
        assert "activation_failed" not in r
        assert self._is_active(others[0]["id"]) is True

    def test_opting_out_leaves_the_screen_alone(self, driver, scratch_session):
        """activate=false still has to re-target the bridge, just without
        stealing the user's foreground tab."""
        S.activate_tab(session_id=scratch_session)
        others = [t for t in S.compact_tabs(fresh=True) if t["id"] != scratch_session]
        if not others:
            pytest.skip("need a second tab")

        r = S.switch_tab(session_id=others[0]["id"], activate=False)
        assert r["active_session_id"] == others[0]["id"]
        assert "activated" not in r
        assert self._is_active(scratch_session) is True  # untouched


class TestCookiesAndStorage:
    def test_cookie_roundtrip(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.set_cookies({"name": "live_test", "value": "v1", "path": "/"},
                          session_id=scratch_session)
        assert r["status"] == "ok", r
        g = S.get_cookies(session_id=scratch_session)
        vals = [c["value"] for c in (g.get("data") or []) if c["name"] == "live_test"]
        assert vals == ["v1"], vals
        d = S.delete_cookies("live_test", session_id=scratch_session)
        assert d["status"] == "ok", d
        g2 = S.get_cookies(session_id=scratch_session)
        assert not [c for c in (g2.get("data") or []) if c["name"] == "live_test"]

    def test_storage_roundtrip(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        s = S.storage_set("lk", "lv", session_id=scratch_session)
        assert s["status"] == "success", s
        r = S.storage_get("lk", session_id=scratch_session)
        assert r["found"] and r["value"] == "lv"

    def test_storage_dump_all(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.storage_set("lk2", "lv2", session_id=scratch_session)
        r = S.storage_get(session_id=scratch_session)
        assert r["status"] == "success"
        assert r["items"].get("lk2") == "lv2"


class TestWaitForUrl:
    def test_waits_for_navigation_to_complete(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.execute_js('location.href="https://example.org/"',
                     session_id=scratch_session, no_monitor=True, timeout=10)
        r = S.wait_for_url(r"example\.org", timeout=20, session_id=scratch_session)
        assert r["status"] == "success", r
        assert "example.org" in r["url"]
        assert r["ready_state"] == "complete"

    def test_substring_pattern_works(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.execute_js('location.href="https://example.org/"',
                     session_id=scratch_session, no_monitor=True, timeout=10)
        r = S.wait_for_url("example.org", timeout=20, session_id=scratch_session)
        assert r["status"] == "success", r

    def test_times_out_when_pattern_never_matches(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for_url(r"definitely-not-here\.xyz", timeout=3,
                           session_id=scratch_session)
        assert r["status"] == "timeout"
        # It must actually have waited, not returned instantly.
        assert r["waited_ms"] >= 2500, r["waited_ms"]
