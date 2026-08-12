from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from agent_browser_mcp import server as S


ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "src/agent_browser_mcp/chrome_extension/background.js"
DISABLE_DIALOGS = ROOT / "src/agent_browser_mcp/chrome_extension/disable_dialogs.js"


def _run_node_harness(source: str) -> dict:
    completed = subprocess.run(
        ["node", "-"],
        input=source,
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _exec_script_harness(code: str, scope: dict | None) -> dict:
    return _run_node_harness(
        f"""
const fs = require('fs');
const vm = require('vm');
const background = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const disableDialogs = fs.readFileSync({json.dumps(str(DISABLE_DIALOGS))}, 'utf8');
const builderStart = background.indexOf('function buildExecScript');
const builderEnd = background.indexOf('\\nfunction buildPageScript', builderStart);
if (builderStart < 0 || builderEnd < 0) throw new Error('buildExecScript not found');
eval(background.slice(builderStart, builderEnd));
let nativeConfirmCalls = 0;
const document = {{
  createElement() {{ return {{ style: {{}}, remove() {{}}, textContent: '' }}; }},
  body: {{ appendChild() {{}} }},
  documentElement: {{ appendChild() {{}} }},
}};
const window = {{
  document,
  alert() {{}},
  confirm() {{ nativeConfirmCalls += 1; return true; }},
  prompt(_msg, value) {{ return value ?? ''; }},
}};
window.window = window;
const context = vm.createContext({{
  window, document,
  console: {{ log() {{}} }},
  setTimeout() {{ return 0; }},
  Date, Math, JSON, Array, Object, String, Error,
  NodeList: function NodeList() {{}},
  HTMLCollection: function HTMLCollection() {{}},
}});
vm.runInContext(disableDialogs, context);
(async () => {{
  const expression = buildExecScript(
    {json.dumps(code)},
    "return {{ ok: false, error: {{ name: e.name, message: e.message }} }};",
    {json.dumps(scope)},
  );
  let result;
  try {{
    result = await vm.runInContext(expression, context, {{ timeout: 250 }});
  }} catch (error) {{
    if (error?.code !== 'ERR_SCRIPT_EXECUTION_TIMEOUT') throw error;
    const token = {json.dumps(scope.get("token") if scope else None)};
    const dialogs = (Array.isArray(window.__tmwd_dialog_records)
      ? window.__tmwd_dialog_records : []).filter(record => record.token === token);
    result = {{ ok: true, data: {{
      __tmwd_dialog_result: true,
      value: null,
      dialogs,
      manual_blocked: true,
    }} }};
  }}
  process.stdout.write(JSON.stringify({{
    result,
    after: window.after,
    nativeConfirmCalls,
    scopesInstalled: Array.isArray(window.__tmwd_dialog_scopes),
    suppressUntil: window.__tmwd_suppress_until ?? null,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    )


@pytest.mark.parametrize("policy", ["dismiss", "accept", "manual"])
def test_dialog_policy_accepts_only_documented_values(policy):
    assert S._validate_dialog_policy(policy) == policy


@pytest.mark.parametrize("policy", ["", "yes", "ignore", "DISMISS", None, 1])
def test_dialog_policy_rejects_invalid_values_before_bridge(policy):
    with pytest.raises(ValueError, match="dismiss.*accept.*manual"):
        S._validate_dialog_policy(policy)


def test_dismissed_beforeunload_is_classified_as_blocked():
    result = S._classify_navigation_result(
        {
            "status": "ok",
            "dialog": {"type": "beforeunload", "message": "Leave?"},
            "dialog_action": "dismiss",
            "url": "https://old.example/",
        },
        requested_url="https://new.example/",
    )
    assert result["status"] == "blocked_by_beforeunload"
    assert result["url"] == "https://old.example/"


def test_manual_dialog_is_never_reported_as_handled():
    result = S._classify_navigation_result(
        {
            "status": "ok",
            "dialog": {"type": "confirm", "message": "Continue?"},
            "dialog_action": "manual",
        },
        requested_url="https://new.example/",
    )
    assert result["status"] == "blocked_by_dialog"
    assert result["dialog"]["message"] == "Continue?"


@pytest.mark.parametrize(
    "bridge_status",
    ["navigation_timeout", "dialog_handle_failed", "navigation_failed"],
)
def test_navigation_failure_status_is_preserved(bridge_status):
    result = S._classify_navigation_result(
        {
            "status": bridge_status,
            "dialog": {"type": "beforeunload", "message": "Leave?"},
            "dialog_action": "accept",
            "handled": False,
            "handle_error": "failed" if bridge_status == "dialog_handle_failed" else None,
            "url": "https://old.example/",
        },
        requested_url="https://new.example/",
    )
    assert result["status"] == bridge_status


class FakeDriver:
    def __init__(self, response=None):
        self.default_session_id = "other:7"
        self.calls = []
        self.response = response or {"data": {"status": "ok"}}

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.calls.append((payload, client_id, timeout))
        return self.response


def _install_target(monkeypatch, driver, sid="chrome:profile:42"):
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda timeout=None, fresh=False: [{"id": sid, "url": "https://old.example/"}],
    )
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)
    return sid


def test_open_url_uses_directed_navigate_route_and_preserves_full_session(monkeypatch):
    driver = FakeDriver(
        {
            "data": {
                "status": "blocked_by_beforeunload",
                "dialog": {"type": "beforeunload", "message": "Leave?"},
                "dialog_action": "dismiss",
                "url": "https://old.example/",
            }
        }
    )
    sid = _install_target(monkeypatch, driver)

    result = S.open_url(
        "https://new.example/", session_id=sid, beforeunload="dismiss", timeout=9.0
    )

    assert result["status"] == "blocked_by_beforeunload"
    assert driver.default_session_id == "other:7"
    assert len(driver.calls) == 1
    payload, client_id, call_timeout = driver.calls[0]
    assert payload == {
        "cmd": "navigate",
        "tabId": 42,
        "url": "https://new.example/",
        "beforeunload": "dismiss",
        "timeoutMs": payload["timeoutMs"],
    }
    assert client_id == "chrome:profile"
    assert 0 < call_timeout <= 9.0
    assert 0 < payload["timeoutMs"] <= 9000
    assert payload["timeoutMs"] <= int(call_timeout * 1000)


def test_open_url_rejects_invalid_policy_without_touching_bridge(monkeypatch):
    monkeypatch.setattr(
        S, "require_driver", lambda: pytest.fail("bridge must not be touched")
    )
    with pytest.raises(ValueError, match="dismiss.*accept.*manual"):
        S.open_url("https://example.test/", beforeunload="ignore")


def test_handle_dialog_routes_action_and_prompt_to_requested_browser(monkeypatch):
    driver = FakeDriver(
        {
            "data": {
                "status": "ok",
                "dialog": {"type": "prompt", "message": "Name?"},
                "handled": True,
            }
        }
    )
    sid = _install_target(monkeypatch, driver)

    result = S.handle_dialog("accept", prompt_text="ABM", session_id=sid)

    assert result["status"] == "ok"
    assert driver.default_session_id == "other:7"
    assert driver.calls == [
        (
            {
                "cmd": "handle_dialog",
                "tabId": 42,
                "action": "accept",
                "promptText": "ABM",
            },
            "chrome:profile",
            3.0,
        )
    ]


def test_handle_dialog_rejects_invalid_action_without_touching_bridge(monkeypatch):
    monkeypatch.setattr(
        S, "require_driver", lambda: pytest.fail("bridge must not be touched")
    )
    with pytest.raises(ValueError, match="dismiss.*accept.*manual"):
        S.handle_dialog("close")


def test_directed_dead_dialog_session_does_not_fall_through(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda timeout=None, fresh=False: [
            {"id": "chrome:profile:99", "url": "https://live.example/"}
        ],
    )

    with pytest.raises(RuntimeError, match="not found"):
        S.handle_dialog("dismiss", session_id="chrome:profile:42")

    assert driver.calls == []
    assert driver.default_session_id == "other:7"


def test_execute_js_sets_and_clears_per_call_policy_on_failure(monkeypatch):
    driver = FakeDriver({"data": {"token": "scope-1"}})
    sid = _install_target(monkeypatch, driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": sid}])

    def fail(*args, **kwargs):
        raise RuntimeError("script failed")

    monkeypatch.setattr(S.simphtml, "execute_js_rich", fail)

    with pytest.raises(RuntimeError, match="script failed"):
        S.execute_js("throw new Error('x')", session_id=sid, dialog_policy="accept")

    assert driver.calls[0][0] == {
        "cmd": "set_dialog_policy",
        "tabId": 42,
        "policy": "accept",
        "timeoutMs": 15000,
    }
    clear = driver.calls[-1][0]
    assert clear["cmd"] == "clear_dialog_policy"
    assert clear["tabId"] == 42
    assert clear["token"] == "scope-1"
    assert driver.default_session_id == "other:7"


def test_execute_js_surfaces_native_manual_pause_without_a_guessed_value(monkeypatch):
    driver = FakeDriver({"data": {"token": "scope-1"}})
    sid = _install_target(monkeypatch, driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": sid}])
    observed = {
        "type": "confirm",
        "message": "Proceed?",
        "policy": "manual",
        "openedAt": 123,
    }
    rich_calls = []

    def rich(script, *args, **kwargs):
        rich_calls.append({"script": script, **kwargs})
        return {
            "status": "success",
            "js_return": {
                "__tmwd_dialog_result": True,
                "value": None,
                "dialogs": [observed],
                "status": "blocked_by_dialog",
                "handled": False,
                "pending_execution": True,
            },
            "tab_id": 42,
        }

    monkeypatch.setattr(S.simphtml, "execute_js_rich", rich)

    result = S.execute_js("confirm('Proceed?')", session_id=sid, dialog_policy="manual")

    assert result["status"] == "blocked_by_dialog"
    assert result["js_return"] is None
    assert result["manual_blocked"] is True
    assert result["handled"] is False
    assert result["pending_execution"] is True
    assert result["dialog"] == observed
    assert result["dialogs"] == [observed]
    assert rich_calls[0]["no_monitor"] is False
    assert rich_calls[0]["script"] == "confirm('Proceed?')"
    set_policy = driver.calls[0][0]
    assert set_policy["source"] == "confirm('Proceed?')"


def test_execute_js_rich_skips_only_post_monitor_for_native_dialog_pause(monkeypatch):
    class MonitorDriver:
        default_session_id = "chrome:profile:42"

        def execute_js(self, script, timeout=15):
            return {
                "data": {
                    "__tmwd_dialog_result": True,
                    "value": None,
                    "status": "blocked_by_dialog",
                    "manual_blocked": True,
                },
                "executed_tab_id": 42,
            }

        def get_session_dict(self, timeout=3):
            pytest.fail("blocked result must not perform a post session roundtrip")

    html_calls = []
    transient_calls = []
    monkeypatch.setattr(
        S.simphtml,
        "get_html",
        lambda *args, **kwargs: html_calls.append(kwargs) or "<main>before</main>",
    )
    monkeypatch.setattr(
        S.simphtml,
        "get_temp_texts",
        lambda *args, **kwargs: transient_calls.append(kwargs) or [],
    )
    monkeypatch.setattr(S.simphtml.time, "sleep", lambda _seconds: None)

    result = S.simphtml.execute_js_rich(
        "confirm('Proceed?')",
        MonitorDriver(),
        no_monitor=False,
        before_sids=set(),
    )

    assert result["js_return"]["status"] == "blocked_by_dialog"
    assert len(html_calls) == 1
    assert transient_calls == []


def test_execute_js_rich_manual_evaluation_first_keeps_normal_monitor(monkeypatch):
    class MonitorDriver:
        default_session_id = "chrome:profile:42"

        def execute_js(self, script, timeout=15):
            return {"data": 7, "executed_tab_id": 42}

        def get_session_dict(self, timeout=3):
            return {"chrome:profile:42": "https://example.test/"}

    html_calls = []
    transient_calls = []

    def get_html(*args, **kwargs):
        html_calls.append(kwargs)
        return "<main>before</main>" if len(html_calls) == 1 else "<main>after</main>"

    monkeypatch.setattr(S.simphtml, "get_html", get_html)
    monkeypatch.setattr(
        S.simphtml,
        "get_temp_texts",
        lambda *args, **kwargs: transient_calls.append(kwargs) or ["Saved"],
    )
    monkeypatch.setattr(
        S.simphtml,
        "find_changed_elements",
        lambda before, after: {"changed": 1, "top_change": "main"},
    )
    monkeypatch.setattr(S.simphtml.time, "sleep", lambda _seconds: None)

    result = S.simphtml.execute_js_rich(
        "1 + 6",
        MonitorDriver(),
        no_monitor=False,
        before_sids={"chrome:profile:42"},
    )

    assert result["js_return"] == 7
    assert result["transients"] == ["Saved"]
    assert result["diff"].startswith("DOM变化量: 1")
    assert len(html_calls) == 2
    assert len(transient_calls) == 1


def test_manual_policy_does_not_install_a_page_global_dialog_scope():
    outcome = _exec_script_harness(
        "window.after = false;\n"
        "const answer = window.confirm('Native');\n"
        "window.after = true;\n"
        "return answer;",
        {"token": "scope-1", "policy": "manual"},
    )
    assert outcome["nativeConfirmCalls"] == 1
    assert outcome["after"] is True
    assert outcome["result"] == {"ok": True, "data": True}
    assert outcome["scopesInstalled"] is False
    assert outcome["suppressUntil"] is None


def test_unmarked_monitor_script_does_not_install_or_use_a_dialog_scope():
    outcome = _exec_script_harness(
        "window.after = false;\nwindow.confirm('Native');\nwindow.after = true;\nreturn 'done';",
        None,
    )
    assert outcome["after"] is True
    assert outcome["nativeConfirmCalls"] == 1
    assert outcome["result"] == {"ok": True, "data": "done"}
    assert outcome["scopesInstalled"] is False
    assert outcome["suppressUntil"] is None


def test_execute_js_marks_only_the_user_script_with_its_policy_token(monkeypatch):
    driver = FakeDriver({"data": {"token": "scope-123"}})
    sid = _install_target(monkeypatch, driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": sid}])
    seen = []

    def rich(script, *args, **kwargs):
        seen.append(script)
        return {"status": "success", "js_return": 2, "tab_id": 42}

    monkeypatch.setattr(S.simphtml, "execute_js_rich", rich)
    result = S.execute_js("return 1 + 1", session_id=sid)

    assert result["js_return"] == 2
    assert seen == ["/*__tmwd_dialog_scope:scope-123*/\nreturn 1 + 1"]


def test_execute_js_passes_explicit_session_without_parking_shared_default(monkeypatch):
    driver = FakeDriver({"data": {"token": "scope-123"}})
    sid = _install_target(monkeypatch, driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": sid}])
    rich_calls = []

    def rich(script, *args, **kwargs):
        rich_calls.append((script, kwargs, driver.default_session_id))
        return {"status": "success", "js_return": 2, "tab_id": 42}

    monkeypatch.setattr(S.simphtml, "execute_js_rich", rich)
    result = S.execute_js("return 1 + 1", session_id=sid, no_monitor=True)

    assert result["js_return"] == 2
    assert rich_calls[0][1]["session_id"] == sid
    assert rich_calls[0][2] == "other:7"
    assert driver.default_session_id == "other:7"


def test_execute_js_restores_default_even_when_policy_cleanup_fails(monkeypatch):
    class CleanupFailureDriver(FakeDriver):
        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            if payload["cmd"] == "clear_dialog_policy":
                raise RuntimeError("cleanup failed")
            return {"data": {"token": "scope-1"}}

    driver = CleanupFailureDriver()
    sid = _install_target(monkeypatch, driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": sid}])
    monkeypatch.setattr(
        S.simphtml,
        "execute_js_rich",
        lambda *args, **kwargs: {"status": "success", "js_return": 1, "tab_id": 42},
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        S.execute_js("return 1", session_id=sid)

    assert driver.default_session_id == "other:7"


def test_policy_cleanup_failure_does_not_mask_script_failure(monkeypatch):
    class BothFailDriver(FakeDriver):
        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            if payload["cmd"] == "clear_dialog_policy":
                raise RuntimeError("cleanup failed")
            return {"data": {"token": "scope-1"}}

    driver = BothFailDriver()
    sid = _install_target(monkeypatch, driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": sid}])
    monkeypatch.setattr(
        S.simphtml,
        "execute_js_rich",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("script failed")),
    )

    with pytest.raises(ValueError, match="script failed"):
        S.execute_js("throw new Error('x')", session_id=sid)

    assert driver.default_session_id == "other:7"


def test_extension_contains_bounded_protocol_dialog_state():
    source = BACKGROUND.read_text(encoding="utf-8")
    for contract in (
        "Page.javascriptDialogOpening",
        "Page.javascriptDialogClosed",
        "Page.handleJavaScriptDialog",
        "dialog_state",
        "handle_dialog",
        "navigate",
        "openedAt",
        "30000",
    ):
        assert contract in source


def test_extension_applies_policy_only_to_the_token_marked_script():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "__tmwd_dialog_scope:" in source
    assert "dialogScopeToken" in source


def _native_manual_cdp_harness(
    mode: str, code: str = 'confirm("Proceed?")'
) -> dict:
    return _run_node_harness(
        f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
function section(startMarker, endMarker) {{
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  if (start < 0 || end < 0) throw new Error('missing section: ' + startMarker);
  return source.slice(start, end);
}}

const protocolDialogStates = new Map();
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const pendingNavigations = new Map();
const pendingManualExecutions = new Map();
const execDialogPolicies = new Map();
const runtimeExecutionContexts = new Map();
const runtimeContextWaiters = new Map();
const dialogEventSequences = new Map();
const manualExecutionGenerations = new Map();
let nextManualExecutionGeneration = 1;
const DIALOG_STATE_TTL_MS = 30000;
function validDialogPolicy(policy) {{
  return policy === 'dismiss' || policy === 'accept' || policy === 'manual';
}}
function currentProtocolDialog(tabId) {{
  return protocolDialogStates.get(tabId) || null;
}}
function rememberProtocolDialog(tabId, params) {{
  const dialog = {{
    type: params.type,
    message: params.message || '',
    url: params.url || '',
    defaultPrompt: params.defaultPrompt || '',
    openedAt: Date.now(),
  }};
  protocolDialogStates.set(tabId, dialog);
  return dialog;
}}
function rememberRuntimeExecutionContext() {{}}
function forgetRuntimeExecutionContext() {{}}
function buildCdpScript() {{ return 'ordinary-main-world-expression'; }}
async function waitForDefaultRuntimeExecutionContext() {{ return 101; }}

let attachCalls = 0;
let detachCalls = 0;
let evaluationStarted = false;
let evaluationSettled = false;
let barrierSeen = false;
let barrierCount = 0;
let userEvaluationCount = 0;
let resolveEvaluation;
let debuggerDetachListener = null;
let tabRemovedListener = null;
const commands = [];
const userEvaluations = [];
const evaluationGate = new Promise(resolve => {{ resolveEvaluation = resolve; }});
const chrome = {{
  debugger: {{
    onDetach: {{ addListener(listener) {{ debuggerDetachListener = listener; }} }},
    attach() {{ attachCalls += 1; return Promise.resolve(); }},
    detach() {{ detachCalls += 1; return Promise.resolve(); }},
    sendCommand(target, method, params = {{}}) {{
      commands.push({{ tabId: target.tabId, method, params }});
      if (method === 'Runtime.enable' || method === 'Page.enable') return Promise.resolve({{}});
      if (method === 'Page.getFrameTree') return Promise.resolve({{
        frameTree: {{ frame: {{ id: 'main-frame' }} }},
      }});
      if (method === 'Runtime.evaluate') {{
        if (params.expression === 'void 0') {{
          barrierSeen = true;
          barrierCount += 1;
          if ({json.dumps(mode)} === 'stale') {{
            handleDebuggerEvent({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
              type: 'confirm', message: 'stale before current',
            }});
          }}
          if ({json.dumps(mode)} === 'generation' && barrierCount === 2) {{
            handleDebuggerEvent({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
              type: 'confirm', message: 'old generation',
            }});
          }}
          return Promise.resolve({{ result: {{ value: undefined }} }});
        }}
        evaluationStarted = true;
        userEvaluationCount += 1;
        userEvaluations.push(params);
        if ({json.dumps(mode)} === 'generation') {{
          if (userEvaluationCount === 1) {{
            evaluationSettled = true;
            return Promise.resolve({{ result: {{ value: 1 }} }});
          }}
          queueMicrotask(() => {{
            if (barrierCount < 2) {{
              handleDebuggerEvent({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
                type: 'confirm', message: 'old generation',
              }});
            }}
            handleDebuggerEvent({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
              type: 'confirm', message: 'current generation',
            }});
          }});
          return evaluationGate.then(value => {{
            evaluationSettled = true;
            return value;
          }});
        }}
        if ({json.dumps(mode)} === 'evaluation') {{
          evaluationSettled = true;
          return Promise.resolve({{ result: {{ value: 7 }} }});
        }}
        if ({json.dumps(mode)} === 'failure') {{
          evaluationSettled = true;
          return Promise.reject(new Error('evaluation failed'));
        }}
        if ({json.dumps(mode)} === 'syntax') {{
          evaluationSettled = true;
          return Promise.resolve({{
            exceptionDetails: {{
              text: 'Uncaught SyntaxError: Illegal return statement',
              exception: {{ description: 'SyntaxError: Illegal return statement', className: 'SyntaxError' }},
            }},
          }});
        }}
        queueMicrotask(() => {{
          if ({json.dumps(mode)} === 'ttl') {{
            const pending = pendingManualExecutions.get(42);
            if (pending) pending.deadline = Date.now() - 1;
            handleDebuggerEvent({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
              type: 'confirm', message: 'expired event',
            }});
            return;
          }}
          if ({json.dumps(mode)} === 'stale' && !barrierSeen) {{
            handleDebuggerEvent({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
              type: 'confirm', message: 'stale before current',
            }});
          }}
          handleDebuggerEvent({{ tabId: 43 }}, 'Page.javascriptDialogOpening', {{
            type: 'confirm', message: 'wrong tab',
          }});
          handleDebuggerEvent({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
            type: 'confirm', message: 'Proceed?',
          }});
          handleDebuggerEvent({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
            type: 'confirm', message: 'stale duplicate',
          }});
        }});
        return evaluationGate.then(value => {{
          evaluationSettled = true;
          return value;
        }});
      }}
      if (method === 'Page.handleJavaScriptDialog') {{
        resolveEvaluation({{ result: {{ value: 'continued' }} }});
        return Promise.resolve({{}});
      }}
      throw new Error('unexpected command: ' + method);
    }},
  }},
  tabs: {{
    onRemoved: {{ addListener(listener) {{ tabRemovedListener = listener; }} }},
  }},
}};

eval(section('function handleDebuggerEvent', '\\n\\nchrome.debugger.onEvent.addListener'));
eval(section('function debuggerTargetKey', '\\n\\nasync function handleProtocolDialog'));
eval(section('async function handleProtocolDialog', '\\n\\nfunction classifyNavigationOutcome'));
eval(section('function manualExecutionResult', '\\n// --- Scoped, temporary CSP removal'));
eval(section(
  'chrome.debugger.onDetach.addListener',
  '\\n\\nfunction currentExecDialogPolicy',
));

(async () => {{
  if ({json.dumps(mode)} === 'generation') {{
    const firstGeneration = await executeManualScript(
      42, '1', {{ token: 'scope-0', policy: 'manual', timeoutMs: 1000 }},
    );
    evaluationSettled = false;
    const secondGeneration = await executeManualScript(
      42, {json.dumps(code)},
      {{ token: 'scope-1', policy: 'manual', timeoutMs: 1000 }},
    );
    process.stdout.write(JSON.stringify({{
      firstGeneration, secondGeneration, barrierCount, userEvaluations,
      pendingAtEnd: pendingManualExecutions.has(42), commands,
    }}));
    return;
  }}
  const first = await executeManualScript(
    42, {json.dumps(code)},
    {{ token: 'scope-1', policy: 'manual', timeoutMs: 1000 }},
  );
  const refsAfterFirst = debuggerAttachments.get('tab:42')?.refs || 0;
  const pendingAfterFirst = pendingManualExecutions.has(42);
  const automaticActions = commands.filter(command =>
    command.method === 'Page.handleJavaScriptDialog' ||
    command.method === 'Runtime.terminateExecution'
  );

  if ({json.dumps(mode)} === 'dialog') {{
    const attachBeforeBusy = attachCalls;
    const busy = await executeManualScript(
      42, 'return 2;',
      {{ token: 'scope-2', policy: 'manual', timeoutMs: 1000 }},
    );
    const attachAfterBusy = attachCalls;
    const handled = await handleProtocolDialog({{
      tabId: 42, action: 'accept', promptText: 'ABM',
    }});
    for (let i = 0; i < 20 && pendingManualExecutions.has(42); i += 1) {{
      await new Promise(resolve => setTimeout(resolve, 0));
    }}
    process.stdout.write(JSON.stringify({{
      first, busy, handled, attachBeforeBusy, attachAfterBusy,
      refsAfterFirst, pendingAfterFirst, automaticActions,
      evaluationStarted, evaluationSettled, attachCalls, detachCalls,
      pendingAtEnd: pendingManualExecutions.has(42), commands,
      barrierSeen, userEvaluations,
    }}));
    return;
  }}

  if ({json.dumps(mode)} === 'navigation') {{
    handleDebuggerEvent({{ tabId: 42 }}, 'Page.frameNavigated', {{
      frame: {{ id: 'replacement-frame' }},
    }});
  }} else if ({json.dumps(mode)} === 'detach') {{
    debuggerDetachListener({{ tabId: 42 }});
  }} else if ({json.dumps(mode)} === 'removed') {{
    tabRemovedListener(42);
  }}
  if (['navigation', 'detach', 'removed'].includes({json.dumps(mode)})) {{
    for (let i = 0; i < 20 && pendingManualExecutions.has(42); i += 1) {{
      await new Promise(resolve => setTimeout(resolve, 0));
    }}
  }}

  process.stdout.write(JSON.stringify({{
    first, refsAfterFirst, pendingAfterFirst, automaticActions,
    evaluationStarted, evaluationSettled, attachCalls, detachCalls,
    pendingAtEnd: pendingManualExecutions.has(42), commands,
    barrierSeen, userEvaluations,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    )


def test_manual_policy_uses_native_dialog_pause_without_rewriters_or_termination():
    background = BACKGROUND.read_text(encoding="utf-8")
    wrapper = DISABLE_DIALOGS.read_text(encoding="utf-8")
    manual = background[
        background.index("function manualExecutionResult"):
        background.index("// --- Scoped, temporary CSP removal")
    ]
    assert "Runtime.terminateExecution" not in background
    assert "__TMWD_MANUAL_DIALOG_STOP__" not in background
    assert "__TMWD_MANUAL_DIALOG_STOP__" not in wrapper
    assert "buildManualCdpScript" not in background
    assert "__tmwd_manual_dialog_state" not in background
    assert "while (true)" not in wrapper
    assert "Runtime.evaluate" in manual
    assert "buildCdpScript(code, null)" not in manual
    assert "expression: code" in manual
    assert "replMode: true" in manual
    assert "Page.createIsolatedWorld" not in manual
    assert "waitForDefaultRuntimeExecutionContext" in manual


@pytest.mark.parametrize(
    "code",
    [
        "1 + 6",
        "(0, eval)('3 + 4')",
        "Function('return 7')()",
        "14 / 2",
        "/ab+/.test('abbb')",
        "`value:${3 + 4}`",
        "await Promise.resolve(7)",
        "(async () => { return await Promise.resolve(7); })()",
    ],
)
def test_native_manual_runtime_evaluate_preserves_authored_source(code):
    outcome = _native_manual_cdp_harness("evaluation", code)
    assert outcome["first"] == {"ok": True, "data": 7}
    assert outcome["barrierSeen"] is True
    assert len(outcome["userEvaluations"]) == 1
    evaluation = outcome["userEvaluations"][0]
    assert evaluation["expression"] == code
    assert evaluation["replMode"] is True
    assert evaluation["awaitPromise"] is True
    assert evaluation["returnByValue"] is True


def test_native_manual_top_level_return_fails_closed_with_rewrite_guidance():
    outcome = _native_manual_cdp_harness("syntax", "return 7")
    result = outcome["first"]["data"]
    assert result["status"] == "manual_raw_syntax_error"
    assert result["pending_execution"] is False
    assert result["error"]["code"] == "manual_raw_syntax_error"
    assert "raw expression" in result["error"]["suggestion"]


def test_native_manual_dialog_first_returns_pending_and_explicit_accept_releases_owner():
    outcome = _native_manual_cdp_harness("dialog")
    first = outcome["first"]["data"]
    assert first["status"] == "blocked_by_dialog"
    assert first["handled"] is False
    assert first["pending_execution"] is True
    assert first["dialog"]["message"] == "Proceed?"
    assert outcome["evaluationStarted"] is True
    assert outcome["barrierSeen"] is True
    assert outcome["userEvaluations"][0]["expression"] == 'confirm("Proceed?")'
    assert outcome["pendingAfterFirst"] is True
    assert outcome["refsAfterFirst"] == 1
    assert outcome["automaticActions"] == []
    assert outcome["busy"]["data"]["status"] == "busy"
    assert outcome["attachAfterBusy"] == outcome["attachBeforeBusy"]
    assert outcome["handled"]["data"]["handled"] is True
    assert outcome["evaluationSettled"] is True
    assert outcome["pendingAtEnd"] is False
    assert outcome["attachCalls"] == 1
    assert outcome["detachCalls"] == 1
    actions = [
        command for command in outcome["commands"]
        if command["method"] == "Page.handleJavaScriptDialog"
    ]
    assert actions == [{
        "tabId": 42,
        "method": "Page.handleJavaScriptDialog",
        "params": {"accept": True, "promptText": "ABM"},
    }]
    assert not any(
        command["method"] == "Runtime.terminateExecution"
        for command in outcome["commands"]
    )


def test_native_manual_stale_before_current_is_ignored_after_cdp_barrier():
    outcome = _native_manual_cdp_harness("stale")
    first = outcome["first"]["data"]
    assert first["status"] == "blocked_by_dialog"
    assert first["dialog"]["message"] == "Proceed?"


def test_native_manual_expired_event_cancels_instead_of_resolving_blocked():
    outcome = _native_manual_cdp_harness("ttl")
    assert outcome["first"]["ok"] is False
    assert "expired" in outcome["first"]["error"]["message"]
    assert outcome["pendingAtEnd"] is False


def test_native_manual_cross_generation_event_cannot_resolve_new_generation():
    outcome = _native_manual_cdp_harness("generation", "confirm('current')")
    assert outcome["firstGeneration"] == {"ok": True, "data": 1}
    assert outcome["secondGeneration"]["data"]["status"] == "blocked_by_dialog"
    assert outcome["secondGeneration"]["data"]["dialog"]["message"] == "current generation"
    assert outcome["barrierCount"] == 2
    assert outcome["pendingAtEnd"] is True


def _manual_navigation_cdp_harness(action: str) -> dict:
    return _run_node_harness(
        f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const protocolDialogStates = new Map();
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const pendingNavigations = new Map();
const pendingManualExecutions = new Map();
const execDialogPolicies = new Map();
const runtimeExecutionContexts = new Map();
const runtimeContextWaiters = new Map();
const dialogEventSequences = new Map();
const DIALOG_STATE_TTL_MS = 30000;
function validDialogPolicy(policy) {{
  return policy === 'dismiss' || policy === 'accept' || policy === 'manual';
}}
function currentProtocolDialog(tabId) {{ return protocolDialogStates.get(tabId) || null; }}
function rememberProtocolDialog(tabId, params) {{
  const dialog = {{ type: params.type, message: params.message || '', url: params.url || '',
    defaultPrompt: params.defaultPrompt || '', openedAt: Date.now() }};
  protocolDialogStates.set(tabId, dialog);
  return dialog;
}}
function debuggerTargetKey(target) {{ return `tab:${{target.tabId}}`; }}
let onEventListener = null;
let onDetachListener = null;
let onRemovedListener = null;
let attachCalls = 0;
let detachCalls = 0;
const handleRefs = [];
let navigationSettled = false;
let resolveNavigation;
const navigationGate = new Promise(resolve => {{ resolveNavigation = resolve; }});
const commands = [];
const chrome = {{
  debugger: {{
    onEvent: {{ addListener(listener) {{ onEventListener = listener; }} }},
    onDetach: {{ addListener(listener) {{ onDetachListener = listener; }} }},
    attach() {{ attachCalls += 1; return Promise.resolve(); }},
    detach() {{ detachCalls += 1; return Promise.resolve(); }},
    sendCommand(target, method, params = {{}}) {{
      commands.push({{ tabId: target.tabId, method, params }});
      if (method === 'Page.enable') return Promise.resolve({{}});
      if (method === 'Page.navigate') {{
        queueMicrotask(() => onEventListener({{ tabId: 42 }}, 'Page.javascriptDialogOpening', {{
          type: 'beforeunload', message: 'Leave?', url: 'https://old.example/',
        }}));
        return navigationGate.then(value => {{ navigationSettled = true; return value; }});
      }}
      if (method === 'Page.handleJavaScriptDialog') {{
        const refs = debuggerAttachments.get('tab:42')?.refs || 0;
        handleRefs.push(refs);
        if (refs !== 1) return Promise.reject(new Error('Detached while handling command.'));
        resolveNavigation({{ frameId: 'new-frame' }});
        queueMicrotask(() => onEventListener({{ tabId: 42 }}, 'Page.javascriptDialogClosed', {{}}));
        return Promise.resolve({{}});
      }}
      throw new Error('unexpected command: ' + method);
    }},
  }},
  tabs: {{
    onRemoved: {{ addListener(listener) {{ onRemovedListener = listener; }} }},
    async get() {{
      return navigationSettled
        ? {{ url: 'https://new.example/', pendingUrl: '', title: 'New' }}
        : {{ url: 'https://old.example/', pendingUrl: '', title: 'Old' }};
    }},
  }},
}};
eval(source.slice(
  source.indexOf('function handleDebuggerEvent'),
  source.indexOf('async function handleExtMessage'),
));
(async () => {{
  const firstPromise = navigateWithDialogPolicy({{
    tabId: 42, url: 'https://new.example/', beforeunload: 'manual', timeoutMs: 1000,
  }});
  const first = await firstPromise;
  const pendingAfterFirst = pendingNavigations.has(42);
  const refsAfterFirst = debuggerAttachments.get('tab:42')?.refs || 0;
  const automaticHandlesBeforeExplicit = commands.filter(
    command => command.method === 'Page.handleJavaScriptDialog'
  ).length;
  const handled = await handleProtocolDialog({{
    tabId: 42, action: {json.dumps(action)},
  }});
  for (let i = 0; i < 50 && pendingNavigations.has(42); i += 1) {{
    await new Promise(resolve => setTimeout(resolve, 0));
  }}
  process.stdout.write(JSON.stringify({{
    first, pendingAfterFirst, refsAfterFirst, automaticHandlesBeforeExplicit, handled,
    pendingAtEnd: pendingNavigations.has(42), attachCalls, detachCalls,
    navigationSettled, commands, handleRefs,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    )


@pytest.mark.parametrize("action", ["accept", "dismiss"])
def test_beforeunload_manual_keeps_owner_until_explicit_handle(action):
    outcome = _manual_navigation_cdp_harness(action)
    assert outcome["first"]["data"]["status"] == "blocked_by_dialog"
    assert outcome["first"]["data"]["handled"] is False
    assert outcome["first"]["data"]["pending_execution"] is True
    assert outcome["pendingAfterFirst"] is True
    assert outcome["refsAfterFirst"] == 1
    assert outcome["automaticHandlesBeforeExplicit"] == 0
    assert outcome["handled"]["data"]["handled"] is True
    assert outcome["pendingAtEnd"] is False
    assert outcome["attachCalls"] == 1
    assert outcome["detachCalls"] == 1
    assert outcome["handleRefs"] == [1]
    handles = [
        c for c in outcome["commands"]
        if c["method"] == "Page.handleJavaScriptDialog"
    ]
    assert handles == [{
        "tabId": 42,
        "method": "Page.handleJavaScriptDialog",
        "params": {"accept": action == "accept"},
    }]


@pytest.mark.parametrize("mode", ["evaluation", "failure"])
def test_native_manual_evaluation_settlement_clears_pending_and_owning_lease(mode):
    outcome = _native_manual_cdp_harness(mode)
    assert outcome["evaluationStarted"] is True
    assert outcome["evaluationSettled"] is True
    assert outcome["pendingAfterFirst"] is False
    assert outcome["refsAfterFirst"] == 0
    assert outcome["pendingAtEnd"] is False
    assert outcome["attachCalls"] == 1
    assert outcome["detachCalls"] == 1
    if mode == "evaluation":
        assert outcome["first"] == {"ok": True, "data": 7}
    else:
        assert outcome["first"]["ok"] is False
        assert "evaluation failed" in outcome["first"]["error"]["message"]


@pytest.mark.parametrize(
    ("mode", "expected_detaches"),
    [("navigation", 1), ("detach", 0), ("removed", 1)],
)
def test_native_manual_external_cleanup_clears_only_the_owning_lease(
    mode, expected_detaches
):
    outcome = _native_manual_cdp_harness(mode)
    assert outcome["first"]["data"]["status"] == "blocked_by_dialog"
    assert outcome["pendingAfterFirst"] is True
    assert outcome["refsAfterFirst"] == 1
    assert outcome["pendingAtEnd"] is False
    assert outcome["attachCalls"] == 1
    assert outcome["detachCalls"] == expected_detaches
    assert outcome["automaticActions"] == []


def test_same_tab_debugger_leases_share_attach_and_preserve_tracking():
    outcome = _run_node_harness(
        f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const fallbackStart = source.indexOf('async function attachAbmDebugger');
const helperStart = start >= 0 ? start : fallbackStart;
const end = source.indexOf('\\n\\nasync function handleProtocolDialog', helperStart);
if (helperStart < 0 || end < 0) throw new Error('debugger lease helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
let attachCalls = 0;
let detachCalls = 0;
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    if (attachCalls > 1) return Promise.reject(new Error('already attached'));
    return Promise.resolve();
  }},
  detach() {{ detachCalls += 1; return Promise.resolve(); }},
}} }};
eval(source.slice(helperStart, end));
(async () => {{
  const first = await attachAbmDebugger({{ tabId: 42 }});
  const second = await attachAbmDebugger({{ tabId: 42 }});
  const trackedAfterSecond = dialogAttachedTabs.has(42);
  await detachAbmDebugger(second);
  const trackedAfterOneRelease = dialogAttachedTabs.has(42);
  await detachAbmDebugger(first);
  process.stdout.write(JSON.stringify({{
    attachCalls, detachCalls, trackedAfterSecond, trackedAfterOneRelease,
    trackedAfterFinalRelease: dialogAttachedTabs.has(42),
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    )
    assert outcome == {
        "attachCalls": 1,
        "detachCalls": 1,
        "trackedAfterSecond": True,
        "trackedAfterOneRelease": True,
        "trackedAfterFinalRelease": False,
    }


def test_manual_cleanup_hooks_cover_detach_tab_removal_and_main_frame_replacement():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert source.count("cancelManualExecution(") >= 4
    detach = source[
        source.index("chrome.debugger.onDetach.addListener"):
        source.index("chrome.tabs.onRemoved.addListener")
    ]
    removed = source[
        source.index("chrome.tabs.onRemoved.addListener"):
        source.index("function currentExecDialogPolicy")
    ]
    handler = source[
        source.index("function handleDebuggerEvent"):
        source.index("chrome.debugger.onEvent.addListener")
    ]
    assert "cancelManualExecution" in detach
    assert "cancelManualExecution" in removed
    assert "Page.frameNavigated" in handler
    assert "cancelManualExecution" in handler


def test_navigation_classifier_rejects_timeout_and_handle_failure():
    outcome = _run_node_harness(
        f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function classifyNavigationOutcome');
const end = source.indexOf('\\nasync function navigateWithDialogPolicy', start);
if (start < 0 || end < 0) throw new Error('classifyNavigationOutcome not found');
eval(source.slice(start, end));
const timeout = classifyNavigationOutcome({{
  firstKind: 'timeout', navigationKind: 'timeout', dialog: null,
  action: 'accept', handleError: null,
}});
const handleFailure = classifyNavigationOutcome({{
  firstKind: 'dialog', navigationKind: 'navigation',
  dialog: {{ type: 'beforeunload' }}, action: 'accept',
  handleError: 'Page.handleJavaScriptDialog failed',
}});
process.stdout.write(JSON.stringify({{ timeout, handleFailure }}));
"""
    )
    assert outcome["timeout"] == "navigation_timeout"
    assert outcome["handleFailure"] == "dialog_handle_failed"


def test_extension_dialog_debugger_paths_detach_in_finally():
    source = BACKGROUND.read_text(encoding="utf-8")
    for function_name in ("handleProtocolDialog", "navigateWithDialogPolicy"):
        match = re.search(
            rf"async function {function_name}\b[\s\S]*?\n}}\n", source
        )
        assert match, function_name
        body = match.group(0)
        assert "finally" in body
        assert "detachAbmDebugger" in body
    manual = source[
        source.index("function manualExecutionResult"):
        source.index("// --- Scoped, temporary CSP removal")
    ]
    assert "releaseManualExecution" in manual
    assert "detachAbmDebugger" in manual


def test_all_extension_debugger_paths_use_state_tracking_wrappers():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "async function attachAbmDebugger" in source
    assert "async function detachAbmDebugger" in source
    assert source.count("chrome.debugger.attach(") == 1
    assert source.count("chrome.debugger.detach(") == 1
    assert "dialogAttachedTabs.add" in source
    assert "dialogAttachedTabs.delete" in source


def test_late_attach_and_manual_results_are_explicit():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "dialog_observed" in source
    assert "status: captured ? 'blocked_by_dialog' : 'no_dialog'" in source
    manual = source.split("if (action === 'manual')", 1)[1].split("}", 1)[0]
    assert "handled: false" in manual


def test_injected_confirm_has_no_unconditional_accept():
    source = DISABLE_DIALOGS.read_text(encoding="utf-8")
    confirm = source.split("window.confirm = function", 1)[1].split(
        "window.prompt = function", 1
    )[0]
    assert "return true" not in confirm
    assert "policy === 'accept'" in confirm or 'policy === "accept"' in confirm


def test_injected_dialog_observations_are_bounded_and_policy_driven():
    source = DISABLE_DIALOGS.read_text(encoding="utf-8")
    assert "__tmwd_dialog_records" in source
    assert "slice(" in source or "shift()" in source
    assert "defaultPrompt" in source
    assert "openedAt" in source
