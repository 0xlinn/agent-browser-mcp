from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from agent_browser_mcp import server as S


BACKGROUND = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agent_browser_mcp"
    / "chrome_extension"
    / "background.js"
)


class _Driver:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.calls.append((payload, client_id, timeout))
        if self.responses:
            return self.responses.pop(0)
        return {"data": {"ok": True}}


def _install(monkeypatch, responses=None):
    driver = _Driver(responses)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    return driver


def test_uninstall_extension_forwards_confirmation_and_client(monkeypatch):
    driver = _install(monkeypatch)
    result = S.uninstall_extension(
        "target-id", show_confirm_dialog=False, session_id="chrome:profile:42"
    )
    assert result == {
        "status": "ok",
        "operation": "uninstall_extension",
        "extension_id": "target-id",
        "confirmation_requested": False,
    }
    assert driver.calls == [
        (
            {
                "cmd": "management",
                "method": "uninstall",
                "extId": "target-id",
                "showConfirmDialog": False,
            },
            "chrome:profile",
            20.0,
        )
    ]


def test_bookmark_tools_match_existing_extension_protocol(monkeypatch):
    driver = _install(
        monkeypatch,
        [
            {"data": {"ok": True, "data": [{"id": "0"}]}},
            {"data": {"ok": True, "data": {"id": "9", "title": "ABM"}}},
            {"data": {"ok": True}},
        ],
    )
    tree = S.get_bookmarks(session_id="chrome:profile:42")
    created = S.create_bookmark(
        "ABM", "https://example.test/", "1", session_id="chrome:profile:42"
    )
    removed = S.remove_bookmark("9", recursive=True, session_id="chrome:profile:42")
    assert tree["data"] == [{"id": "0"}]
    assert created["data"]["id"] == "9"
    assert removed["status"] == "ok"
    assert [call[0] for call in driver.calls] == [
        {"cmd": "bookmarks", "method": "tree"},
        {
            "cmd": "bookmarks",
            "method": "create",
            "node": {
                "title": "ABM",
                "url": "https://example.test/",
                "parentId": "1",
            },
        },
        {"cmd": "bookmarks", "method": "removeTree", "id": "9"},
    ]


def test_call_extension_parses_json_and_preserves_structured_failure(monkeypatch):
    driver = _install(
        monkeypatch,
        [
            {"data": {"ok": True, "data": {"answer": 2}}},
            {
                "data": {
                    "ok": False,
                    "code": "extension_message_failed",
                    "error": "Receiving end does not exist",
                    "hint": "configure externally_connectable",
                }
            },
        ],
    )
    success = S.call_extension("target-id", json.dumps({"ping": 1}))
    failure = S.call_extension("target-id", "null")
    assert success["data"] == {"answer": 2}
    assert driver.calls[0][0]["message"] == {"ping": 1}
    assert driver.calls[1][0]["message"] is None
    assert failure["status"] == "error"
    assert failure["code"] == "extension_message_failed"
    assert "externally_connectable" in failure["hint"]


def test_call_extension_rejects_invalid_json_before_bridge(monkeypatch):
    driver = _install(monkeypatch)
    with pytest.raises(ValueError, match="valid JSON"):
        S.call_extension("target-id", "{broken")
    assert driver.calls == []


def test_extension_source_routes_cross_extension_messages_and_blocks_self_uninstall():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "msg.cmd === 'call_extension'" in source
    assert "chrome.runtime.sendMessage(msg.extId, msg.message)" in source
    assert "externally_connectable" in source
    management = source[
        source.index("if (msg.cmd === 'management')") : source.index(
            "if (msg.cmd === 'bookmarks')"
        )
    ]
    assert "self_uninstall_unsupported" in management


def test_capture_tools_route_to_the_requested_tab_and_flatten_results(monkeypatch):
    driver = _install(
        monkeypatch,
        [
            {"data": {"ok": True, "data": {"status": "capturing", "max_entries": 25}}},
            {"data": {"ok": True, "data": {"status": "stopped", "requests": []}}},
            {"data": {"ok": True, "data": {"status": "capturing", "max_entries": 30}}},
            {"data": {"ok": True, "data": {"status": "capturing", "messages": []}}},
            {"data": {"ok": True, "data": {"status": "stopped", "messages": []}}},
        ],
    )
    driver.default_session_id = "chrome:profile:42"
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda timeout=None, fresh=False: [
            {"id": "chrome:profile:42", "url": "https://example.test/"}
        ],
    )
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)

    started = S.network_capture_start(
        "chrome:profile:42", max_entries=25, max_body_bytes=4096, body_timeout=1
    )
    stopped = S.network_capture_stop("chrome:profile:42")
    console = S.console_capture_start("chrome:profile:42", max_entries=30)
    messages = S.get_console_messages("chrome:profile:42", max_items=10)
    console_stopped = S.console_capture_stop("chrome:profile:42")

    assert started["status"] == "capturing"
    assert stopped["requests"] == []
    assert console["status"] == "capturing"
    assert messages["messages"] == []
    assert console_stopped["status"] == "stopped"
    assert all(call[1] == "chrome:profile" for call in driver.calls)
    assert [call[0]["cmd"] for call in driver.calls] == [
        "network_capture", "network_capture", "console", "console", "console"
    ]
    assert all(call[0]["tabId"] == 42 for call in driver.calls)


def test_capture_tools_flatten_real_remote_bridge_payloads(monkeypatch):
    """The production remote driver already unwraps the extension ok/data envelope."""
    driver = _install(
        monkeypatch,
        [
            {"data": {"status": "capturing", "max_entries": 25}},
            {"data": {"status": "stopped", "requests": [{"url": "https://example.test/api"}]}},
            {"data": {"status": "capturing", "max_entries": 30}},
            {"data": {"status": "capturing", "messages": [{"text": "ABM marker"}]}},
            {"data": {"status": "stopped", "messages": [{"text": "ABM marker"}]}},
        ],
    )
    driver.default_session_id = "chrome:profile:42"
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda timeout=None, fresh=False: [
            {"id": "chrome:profile:42", "url": "https://example.test/"}
        ],
    )
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)

    started = S.network_capture_start("chrome:profile:42", max_entries=25)
    stopped = S.network_capture_stop("chrome:profile:42")
    console = S.console_capture_start("chrome:profile:42", max_entries=30)
    messages = S.get_console_messages("chrome:profile:42", max_items=10)
    console_stopped = S.console_capture_stop("chrome:profile:42")

    assert started["status"] == "capturing"
    assert stopped["status"] == "stopped"
    assert stopped["requests"][0]["url"] == "https://example.test/api"
    assert console["status"] == "capturing"
    assert messages["messages"][0]["text"] == "ABM marker"
    assert console_stopped["status"] == "stopped"


def test_capture_extension_harness_collects_body_console_and_releases_leases():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function boundedCaptureInteger")
    end = source.index("\n\nfunction handleDebuggerEvent", start)
    helpers = source[start:end]
    script = f"""
const networkCaptures = new Map();
const consoleCaptures = new Map();
let attachCalls = 0;
let detachCalls = 0;
const dialogAttachedTabs = new Set();
function boundedCdpTimeout(value, fallback = 20000) {{ return Number(value) || fallback; }}
function debuggerFailureCode() {{ return 'cdp_error'; }}
async function attachAbmDebugger(target) {{
  attachCalls += 1;
  dialogAttachedTabs.add(target.tabId);
  return {{ attachment: {{ target }}, released: false }};
}}
async function detachAbmDebugger(lease) {{ lease.released = true; detachCalls += 1; }}
async function sendDebuggerCommandWithTimeout(_lease, method) {{
  if (method === 'Network.getResponseBody') return {{ body: 'response-body', base64Encoded: false }};
  return {{}};
}}
{helpers}
(async () => {{
  const networkStart = await handleNetworkCaptureCommand({{
    method: 'start', tabId: 42, includeBodies: true, maxEntries: 10,
    maxBodyBytes: 1024, bodyTimeoutMs: 100, timeoutMs: 100,
  }}, {{}});
  handleNetworkCaptureEvent(42, 'Network.requestWillBeSent', {{
    requestId: 'r1', request: {{ url: 'https://example.test/api', method: 'GET', headers: {{}} }},
    type: 'Fetch', timestamp: 1,
  }});
  handleNetworkCaptureEvent(42, 'Network.responseReceived', {{
    requestId: 'r1', response: {{ status: 200, mimeType: 'text/plain', headers: {{}} }},
  }});
  handleNetworkCaptureEvent(42, 'Network.loadingFinished', {{ requestId: 'r1', encodedDataLength: 13 }});
  await Promise.allSettled([...networkCaptures.get(42).pendingBodies]);
  const networkStop = await handleNetworkCaptureCommand({{ method: 'stop', tabId: 42 }}, {{}});

  const consoleStart = await handleConsoleCaptureCommand({{
    method: 'start', tabId: 42, maxEntries: 10, timeoutMs: 100,
  }}, {{}});
  handleConsoleCaptureEvent(42, 'Runtime.consoleAPICalled', {{
    type: 'log', timestamp: 2, args: [{{ value: 'ABM' }}, {{ value: 7 }}],
  }});
  const consoleGet = await handleConsoleCaptureCommand({{
    method: 'get', tabId: 42, offset: 0, maxItems: 10,
  }}, {{}});
  const consoleStop = await handleConsoleCaptureCommand({{ method: 'stop', tabId: 42 }}, {{}});
  process.stdout.write(JSON.stringify({{
    networkStart, networkStop, consoleStart, consoleGet, consoleStop,
    attachCalls, detachCalls,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    request = outcome["networkStop"]["data"]["requests"][0]
    assert request["status"] == 200
    assert request["body"] == "response-body"
    assert outcome["consoleGet"]["data"]["messages"][0]["text"] == "ABM 7"
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2


def test_save_pdf_validates_then_atomically_writes(monkeypatch, tmp_path):
    raw = b"%PDF-1.7\nbody\n%%EOF\n"
    calls = []

    def cdp(method, params_json="{}", **kwargs):
        calls.append((method, json.loads(params_json), kwargs))
        return {
            "status": "ok",
            "data": {"data": __import__("base64").b64encode(raw).decode("ascii")},
            "session_id": "chrome:profile:42",
            "tab_id": 42,
        }

    monkeypatch.setattr(S, "cdp_command", cdp)
    path = tmp_path / "page.pdf"
    result = S.save_pdf(
        str(path),
        session_id="chrome:profile:42",
        landscape=True,
        page_ranges="1-2",
        timeout=4,
    )
    assert path.read_bytes() == raw
    assert result["size"] == len(raw)
    assert result["tab_id"] == 42
    assert calls == [
        (
            "Page.printToPDF",
            {
                "landscape": True,
                "printBackground": True,
                "preferCSSPageSize": True,
                "scale": 1.0,
                "pageRanges": "1-2",
            },
            {"session_id": "chrome:profile:42", "timeout": 4.0},
        )
    ]


@pytest.mark.parametrize("encoded", ["not-base64", "bm90IGEgcGRm"])
def test_save_pdf_rejects_invalid_payload_without_creating_file(
    monkeypatch, tmp_path, encoded
):
    monkeypatch.setattr(
        S,
        "cdp_command",
        lambda *args, **kwargs: {"data": {"data": encoded}},
    )
    path = tmp_path / "bad.pdf"
    with pytest.raises(RuntimeError, match="save_pdf failed"):
        S.save_pdf(str(path))
    assert not path.exists()
