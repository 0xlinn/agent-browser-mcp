from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from agent_browser_mcp import server as S
from agent_browser_mcp.tmwebdriver import Session, TMWebDriver


BACKGROUND = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agent_browser_mcp"
    / "chrome_extension"
    / "background.js"
)


class _Driver:
    def __init__(self, responses=None, default="chrome:profile:7"):
        self.default_session_id = default
        self.responses = list(responses or [])
        self.calls = []

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.calls.append((payload, client_id, timeout))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {"data": {"ok": True}}


def _sessions(sid="chrome:profile:7", url="https://example.test/"):
    return [{"id": sid, "url": url, "browser": "chrome"}]


def _install(monkeypatch, driver, sessions=None):
    sessions = sessions or _sessions()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(
        S, "active_sessions", lambda timeout=None, fresh=False: list(sessions)
    )
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: list(sessions))
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)


def _fresh_tab_ownership(monkeypatch):
    registry = S._TabOwnershipRegistry()
    monkeypatch.setattr(S, "_TAB_OWNERSHIP", registry)
    return registry


def test_automation_profile_defaults_to_lab_and_env_can_force_safe(monkeypatch):
    monkeypatch.delenv("AGENT_BROWSER_MODE", raising=False)
    monkeypatch.delenv("AGENT_BROWSER_LAB_NO_ELICIT", raising=False)
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", None)
    assert S.get_automation_profile()["mode"] == "lab"

    monkeypatch.setenv("AGENT_BROWSER_MODE", "safe")
    assert S.get_automation_profile()["mode"] == "safe"
    assert S.get_automation_profile()["no_elicit"] is False


def test_set_automation_profile_is_process_local_and_validated(monkeypatch):
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", None)
    assert S.set_automation_profile("safe")["mode"] == "safe"
    assert S.set_automation_profile("lab")["mode"] == "lab"
    with pytest.raises(ValueError, match="lab.*safe"):
        S.set_automation_profile("fast")


@pytest.mark.anyio
async def test_lab_no_elicit_skips_physical_prompt_but_keeps_physical_gate(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_MODE", "lab")
    monkeypatch.setenv("AGENT_BROWSER_LAB_NO_ELICIT", "1")
    gate_calls = []

    class Context:
        async def elicit(self, *args, **kwargs):
            pytest.fail("lab no_elicit must not prompt")

    async def run_sync(worker, *args, **kwargs):
        return worker()

    monkeypatch.setattr(S.anyio.to_thread, "run_sync", run_sync)
    monkeypatch.setattr(
        S.physical_input,
        "run_physical_action",
        lambda summary, action: gate_calls.append(summary) or action(),
    )
    monkeypatch.setattr(
        S, "_pyautogui", lambda: SimpleNamespace(moveTo=lambda *args, **kwargs: None)
    )

    result = await S.mouse_move(ctx=Context(), x=1, y=2)
    assert result["status"] == "ok"
    assert len(gate_calls) == 1


def test_storage_timeout_is_structured_and_default_is_30_seconds(monkeypatch):
    def fail(*args, **kwargs):
        assert kwargs["timeout"] == 30.0
        raise TimeoutError("bridge took too long")

    monkeypatch.setattr(S, "exec_js", fail)
    result = S.storage_get()
    assert result["status"] == "error"
    assert result["error_code"] == "timeout"
    assert result["retryable"] is True


def test_storage_dump_has_item_and_byte_bounds(monkeypatch):
    seen = {}

    def execute(script, session_id=None, timeout=30.0):
        seen["script"] = script
        return {
            "data": json.dumps(
                {
                    "ok": True,
                    "items": {"a": "1"},
                    "total_keys": 9,
                    "next_offset": 1,
                    "bytes": 2,
                    "truncated": True,
                }
            )
        }

    monkeypatch.setattr(S, "exec_js", execute)
    result = S.storage_get(offset=0, max_items=1, max_bytes=64)
    assert result["status"] == "success"
    assert result["truncated"] is True
    assert result["next_offset"] == 1
    assert "maxItems = 1" in seen["script"]
    assert "maxBytes = 64" in seen["script"]


def test_lab_host_navigation_auto_accepts_but_intent_false_preserves_dismiss(
    monkeypatch,
):
    monkeypatch.setenv("AGENT_BROWSER_MODE", "lab")
    monkeypatch.setenv("AGENT_BROWSER_AUTO_BEFOREUNLOAD_HOSTS", "shell.,ttyd")
    driver = _Driver(
        [
            {"data": {"status": "ok", "url": "https://new.test/"}},
            {
                "data": {
                    "status": "blocked_by_beforeunload",
                    "url": "https://shell.example/",
                }
            },
        ]
    )
    _install(monkeypatch, driver, _sessions(url="https://shell.example/terminal"))

    automatic = S.open_url("https://new.test/", session_id="chrome:profile:7")
    staying = S.open_url(
        "https://new.test/", session_id="chrome:profile:7", intent_leave=False
    )

    assert driver.calls[0][0]["beforeunload"] == "accept"
    assert automatic["beforeunload_auto"] is True
    assert driver.calls[1][0]["beforeunload"] == "dismiss"
    assert staying["status"] == "blocked_by_beforeunload"
    assert "hint" in staying


def test_open_url_timeout_does_not_repeat_an_unknown_navigation(monkeypatch):
    driver = _Driver([TimeoutError("navigate result timed out")])
    _install(monkeypatch, driver)

    with pytest.raises(TimeoutError, match="navigate result timed out"):
        S.open_url(
            "https://side-effect.test/",
            session_id="chrome:profile:7",
            timeout=0.05,
        )

    assert len(driver.calls) == 1
    assert driver.calls[0][0]["cmd"] == "navigate"


def test_open_url_unknown_command_fallback_uses_one_total_deadline(monkeypatch):
    class DeadlineDriver:
        default_session_id = "chrome:profile:7"

        def __init__(self):
            self.calls = []

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            time.sleep(0.03)
            if payload.get("cmd") == "navigate":
                raise RuntimeError("Unknown cmd: navigate")
            return {"data": {"frameId": "frame-7"}}

    driver = DeadlineDriver()
    _install(monkeypatch, driver)

    result = S.open_url(
        "https://new.test/", session_id="chrome:profile:7", timeout=0.08
    )

    assert result["navigation_mode"] == "cdp_fallback"
    assert len(driver.calls) == 2
    assert driver.calls[1][2] < driver.calls[0][2]
    assert driver.calls[1][0]["timeoutMs"] < driver.calls[0][0]["timeoutMs"]


def test_open_url_session_resolution_uses_one_bounded_snapshot(monkeypatch):
    driver = _Driver([{"data": {"status": "ok", "url": "https://new.test/"}}])
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)
    calls = []

    def sessions(timeout=None, fresh=False):
        calls.append((timeout, fresh))
        time.sleep(0.01)
        return _sessions(url="https://shell.example/terminal")

    monkeypatch.setattr(S, "active_sessions", sessions)
    monkeypatch.setenv("AGENT_BROWSER_MODE", "lab")
    monkeypatch.setenv("AGENT_BROWSER_AUTO_BEFOREUNLOAD_HOSTS", "shell.")

    result = S.open_url(
        "https://new.test/", session_id="chrome:profile:7", timeout=0.08
    )

    assert result["status"] == "ok"
    assert len(calls) == 1
    assert calls[0][1] is True
    assert 0 < calls[0][0] <= 0.08
    assert driver.calls[0][2] < calls[0][0]
    assert driver.calls[0][0]["beforeunload"] == "accept"


def test_open_url_does_not_dispatch_after_session_resolution_exhausts_deadline(
    monkeypatch,
):
    driver = _Driver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)

    def sessions(timeout=None, fresh=False):
        assert 0 < timeout <= 0.01
        assert fresh is True
        time.sleep(0.02)
        return _sessions()

    monkeypatch.setattr(S, "active_sessions", sessions)

    with pytest.raises(TimeoutError, match="during session resolution"):
        S.open_url(
            "https://new.test/", session_id="chrome:profile:7", timeout=0.01
        )

    assert driver.calls == []


def test_execute_js_unknown_policy_command_falls_back_to_direct_cdp(monkeypatch):
    driver = _Driver(
        [
            RuntimeError("Unknown cmd: set_dialog_policy"),
            {
                "data": {
                    "ok": True,
                    "data": {"result": {"value": {"ok": True, "data": 2}}},
                }
            },
        ]
    )
    _install(monkeypatch, driver)
    monkeypatch.setattr(
        S.simphtml,
        "execute_js_rich",
        lambda *args, **kwargs: pytest.fail("page route must not run after fallback"),
    )

    result = S.execute_js(
        "return 1 + 1", session_id="chrome:profile:7", no_monitor=True
    )
    assert result["status"] == "success"
    assert result["js_return"] == 2
    assert result["execution_mode"] == "cdp_fallback"
    assert driver.calls[1][0]["method"] == "Runtime.evaluate"


def test_execute_js_policy_timeout_never_dispatches_fallback_script(monkeypatch):
    driver = _Driver([TimeoutError("policy transport timed out")])
    _install(monkeypatch, driver)
    monkeypatch.setattr(
        S.simphtml,
        "execute_js_rich",
        lambda *args, **kwargs: pytest.fail("user script must not run after policy timeout"),
    )

    with pytest.raises(TimeoutError, match="policy setup"):
        S.execute_js(
            "window.sideEffect = true",
            session_id="chrome:profile:7",
            timeout=0.05,
        )

    assert len(driver.calls) == 1
    assert driver.calls[0][0]["cmd"] == "set_dialog_policy"


def test_direct_cdp_fallback_uses_only_remaining_deadline(monkeypatch):
    class DeadlineDriver:
        default_session_id = "chrome:profile:7"

        def __init__(self):
            self.timeouts = []

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.timeouts.append(("ext", timeout, payload["timeoutMs"]))
            time.sleep(0.03)
            raise RuntimeError("Unknown cmd: cdp")

        def execute_js(self, payload, timeout=15.0, session_id=None):
            self.timeouts.append(("page", timeout, json.loads(payload)["timeoutMs"]))
            return {"data": {"result": {"value": 7}}}

    driver = DeadlineDriver()
    _install(monkeypatch, driver)
    deadline = time.monotonic() + 0.08

    result = S._direct_cdp(
        "Runtime.evaluate",
        {"expression": "7"},
        session_id="chrome:profile:7",
        client_id="chrome:profile",
        tab_id=7,
        timeout=0.08,
        deadline=deadline,
    )

    assert result == {"result": {"value": 7}}
    assert driver.timeouts[1][1] < driver.timeouts[0][1]
    assert driver.timeouts[1][2] < driver.timeouts[0][2]


def test_cdp_command_accepts_full_session_or_numeric_tab(monkeypatch):
    driver = _Driver(
        [
            {"data": {"ok": True, "data": {"value": 1}}},
            {"data": {"ok": True, "data": {"value": 2}}},
        ]
    )
    _install(monkeypatch, driver)

    first = S.cdp_command("Runtime.evaluate", session_id="chrome:profile:7")
    second = S.cdp_command("Runtime.evaluate", tab_id=7)
    assert first["data"]["value"] == 1
    assert second["data"]["value"] == 2
    assert driver.calls[0][0]["tabId"] == 7
    assert driver.calls[0][1] == "chrome:profile"
    assert driver.calls[1][0]["tabId"] == 7


def test_cdp_and_close_tabs_route_explicit_composite_across_default_browser(
    monkeypatch,
):
    driver = _Driver(
        [
            {"data": {"result": {"value": 7}}},
            {"data": {"ok": True}},
        ],
        default="edge:profile:3",
    )
    sessions = [
        {"id": "edge:profile:3", "url": "https://edge.test/", "browser": "edge"},
        {"id": "chrome:profile:7", "url": "https://chrome.test/", "browser": "chrome"},
        {"id": "chrome:profile:8", "url": "https://close.test/", "browser": "chrome"},
    ]
    _install(monkeypatch, driver, sessions)

    cdp = S.cdp_command("Runtime.evaluate", tab_id="chrome:profile:7")
    closed = S.close_tabs("chrome:profile:8", only_if_agent_owned=False)

    assert cdp["data"] == {"result": {"value": 7}}
    assert cdp["session_id"] == "chrome:profile:7"
    assert driver.calls[0][1] == "chrome:profile"
    assert closed["closed"] == 8
    assert driver.calls[1][1] == "chrome:profile"
    assert driver.default_session_id == "edge:profile:3"


def test_close_tabs_accepts_composite_identifiers(monkeypatch):
    driver = _Driver([{"data": {"ok": True}}])
    _install(monkeypatch, driver)
    result = S.close_tabs(
        ["chrome:profile:7", 8], only_if_agent_owned=False
    )
    assert result["closed"] == [7, 8]
    assert driver.calls[0][0]["tabId"] == [7, 8]
    assert driver.calls[0][1] == "chrome:profile"


def test_open_new_tab_returns_ready_session_without_caller_polling(monkeypatch):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True):
            self.calls.append((url, client_id, timeout, active))
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "load_ready": True,
                    "status": "complete",
                }
            }

    driver = NewTabDriver()
    _fresh_tab_ownership(monkeypatch)
    fresh_calls = []

    def sessions(timeout=None, fresh=False):
        fresh_calls.append(fresh)
        return _sessions() + (
            [{"id": "chrome:profile:9", "url": "https://new.test/"}]
            if fresh
            else []
        )

    _install(monkeypatch, driver)
    monkeypatch.setattr(S, "active_sessions", sessions)
    result = S.open_new_tab("https://new.test/", timeout=2.0)
    assert result["tab_id"] == 9
    assert result["session_id"] == "chrome:profile:9"
    assert result["ready"] is True
    assert result["owned"] is False
    assert result["owner_id"] is None
    assert True in fresh_calls


def test_open_new_tab_registers_generation_bound_agent_ownership(monkeypatch):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True):
            self.calls.append((url, client_id, timeout, active))
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-agent",
                    "load_ready": True,
                    "status": "complete",
                }
            }

    driver = NewTabDriver(responses=[{"data": {"ok": True}}])
    _fresh_tab_ownership(monkeypatch)
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://new.test/",
            "browser": "chrome",
            "generation": "generation-agent",
        }
    ]
    _install(monkeypatch, driver, sessions)

    created = S.open_new_tab("https://new.test/", timeout=1.0)
    closed = S.close_tabs(
        created["session_id"], owner_id=created["owner_id"]
    )

    assert created["owned"] is True
    assert created["opener"] == "agent"
    assert created["owner_id"]
    assert closed["closed"] == 9
    assert closed["closed_by"] == "agent"
    assert closed["already_gone"] == []
    assert closed["owner_id"] == created["owner_id"]
    assert driver.calls[-1][0]["expectedGenerations"] == {
        "9": "generation-agent"
    }


def test_close_tabs_default_refuses_preexisting_user_tab(monkeypatch):
    driver = _Driver()
    sessions = [
        {
            "id": "chrome:profile:7",
            "url": "https://user.test/",
            "browser": "chrome",
            "generation": "generation-user",
        }
    ]
    _install(monkeypatch, driver, sessions)
    _fresh_tab_ownership(monkeypatch)

    with pytest.raises(PermissionError, match="not owned by this MCP task"):
        S.close_tabs("chrome:profile:7", owner_id="not-the-owner")

    assert driver.calls == []


def test_two_agent_owner_capabilities_cannot_close_each_other_tabs(monkeypatch):
    driver = _Driver([{"data": {"ok": True}}])
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://agent-a.test/",
            "browser": "chrome",
            "generation": "generation-a",
        }
    ]
    _install(monkeypatch, driver, sessions)
    agent_a = _fresh_tab_ownership(monkeypatch)
    record = agent_a.register(
        "chrome:profile:9", "generation-a", owner_id="agent-a-owner"
    )

    agent_b = S._TabOwnershipRegistry()
    monkeypatch.setattr(S, "_TAB_OWNERSHIP", agent_b)
    with pytest.raises(PermissionError, match="not owned by this MCP task"):
        S.close_tabs("chrome:profile:9", owner_id=record["owner_id"])
    assert driver.calls == []

    monkeypatch.setattr(S, "_TAB_OWNERSHIP", agent_a)
    closed = S.close_tabs("chrome:profile:9", owner_id=record["owner_id"])
    assert closed["closed"] == 9
    assert len(driver.calls) == 1


def test_close_tabs_refuses_reused_native_id_with_new_generation(monkeypatch):
    driver = _Driver()
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://replacement.test/",
            "browser": "chrome",
            "generation": "generation-new",
        }
    ]
    _install(monkeypatch, driver, sessions)
    registry = _fresh_tab_ownership(monkeypatch)
    registry.register(
        "chrome:profile:9", "generation-old", owner_id="agent-owner"
    )

    with pytest.raises(PermissionError, match="lifecycle generation changed"):
        S.close_tabs("chrome:profile:9", owner_id="agent-owner")

    assert driver.calls == []


def test_close_tabs_treats_owned_session_that_is_already_gone_as_user_closed(monkeypatch):
    driver = _Driver([{"data": {"closed": [], "alreadyGone": [9]}}])
    _install(monkeypatch, driver, [])
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [])
    registry = _fresh_tab_ownership(monkeypatch)
    registry.register(
        "chrome:profile:9", "generation-old", owner_id="agent-owner"
    )

    result = S.close_tabs("chrome:profile:9", owner_id="agent-owner")

    assert result["status"] == "already_gone"
    assert result["closed"] == []
    assert result["already_gone"] == 9
    assert result["closed_by"] == "user"
    assert driver.calls[0][0]["tabId"] == 9
    assert driver.calls[0][0]["expectedGenerations"] == {
        "9": "generation-old"
    }


def test_close_tabs_closes_live_owned_tabs_and_skips_user_closed_owned_tabs(monkeypatch):
    driver = _Driver([{"data": {"closed": [10], "alreadyGone": [9]}}])
    sessions = [
        {
            "id": "chrome:profile:10",
            "url": "https://live-agent.test/",
            "browser": "chrome",
            "generation": "generation-live",
        }
    ]
    _install(monkeypatch, driver, sessions)
    registry = _fresh_tab_ownership(monkeypatch)
    registry.register(
        "chrome:profile:9", "generation-gone", owner_id="agent-owner"
    )
    registry.register(
        "chrome:profile:10", "generation-live", owner_id="agent-owner"
    )

    result = S.close_tabs(
        ["chrome:profile:9", "chrome:profile:10"], owner_id="agent-owner"
    )

    assert result["status"] == "ok"
    assert result["closed"] == [10]
    assert result["already_gone"] == [9]
    assert result["closed_by"] == "agent"
    assert driver.calls[0][0]["tabId"] == [9, 10]
    assert driver.calls[0][0]["expectedGenerations"] == {
        "9": "generation-gone",
        "10": "generation-live"
    }


def test_close_tabs_explicit_operator_override_can_close_unowned_tab(monkeypatch):
    driver = _Driver([{"data": {"ok": True}}])
    _install(monkeypatch, driver)
    _fresh_tab_ownership(monkeypatch)

    result = S.close_tabs("chrome:profile:7", only_if_agent_owned=False)

    assert result["closed"] == 7
    assert result["closed_by"] == "none"
    assert result["only_if_agent_owned"] is False
    assert "expectedGenerations" not in driver.calls[0][0]


def test_open_new_tab_does_not_match_same_numeric_id_from_another_browser(
    monkeypatch,
):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "load_ready": True,
                    "status": "complete",
                }
            }

    driver = NewTabDriver(default="chrome:profile:7")
    sessions = [
        {"id": "chrome:profile:7", "url": "https://old.test/", "browser": "chrome"},
        {"id": "edge:profile:9", "url": "https://wrong.test/", "browser": "edge"},
    ]
    _install(monkeypatch, driver, sessions)

    result = S.open_new_tab("https://new.test/", timeout=0.11)

    assert result["ready"] is False
    assert result["status"] == "pending"
    assert result["session_id"] == "chrome:profile:9"
    assert result["url"] == "https://new.test/"


def test_open_new_tab_pending_still_returns_owned_cleanup_capability(monkeypatch):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-pending",
                    "load_ready": False,
                    "status": "loading",
                }
            }

    driver = NewTabDriver(default="chrome:profile:7")
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, _sessions())

    result = S.open_new_tab("chrome://extensions/", timeout=0.01)

    assert result["ready"] is False
    assert result["status"] == "pending"
    assert result["session_id"] == "chrome:profile:9"
    assert result["owned"] is True
    assert result["owner_id"]


def test_open_new_tab_uses_actual_extension_client_with_zero_scriptable_sessions(
    monkeypatch,
):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True):
            assert client_id is None
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-zero-session",
                    "load_ready": False,
                    "status": "loading",
                },
                "client_id": "chrome:profile",
            }

    driver = NewTabDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [])
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [])

    result = S.open_new_tab("https://new.test/", timeout=0.01)

    assert result["status"] == "pending"
    assert result["session_id"] == "chrome:profile:9"
    assert result["owned"] is True
    assert result["owner_id"]


def test_open_new_tab_does_not_make_an_unbounded_tabs_snapshot_after_deadline(
    monkeypatch,
):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-pending",
                    "load_ready": False,
                    "status": "loading",
                }
            }

    driver = NewTabDriver(default="chrome:profile:7")
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, _sessions())
    monkeypatch.setattr(
        S,
        "compact_tabs",
        lambda *args, **kwargs: pytest.fail("deadline tail must not re-query tabs"),
    )

    result = S.open_new_tab("chrome://extensions/", timeout=0.01)

    assert result["status"] == "pending"
    assert "tabs" not in result


def test_open_new_tab_waits_for_the_returned_tab_generation(monkeypatch):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-new",
                    "load_ready": True,
                    "status": "complete",
                }
            }

    driver = NewTabDriver(default="chrome:profile:7")
    fresh_calls = []

    def sessions(timeout=None, fresh=False):
        fresh_calls.append(fresh)
        generation = "generation-old" if len(fresh_calls) == 1 else "generation-new"
        return [
            {"id": "chrome:profile:7", "url": "https://old.test/", "browser": "chrome"},
            {
                "id": "chrome:profile:9",
                "url": "https://new.test/",
                "browser": "chrome",
                "generation": generation,
            },
        ]

    _install(monkeypatch, driver)
    monkeypatch.setattr(S, "active_sessions", sessions)

    result = S.open_new_tab("https://new.test/", timeout=1.0)

    assert result["ready"] is True
    assert result["generation"] == "generation-new"
    assert len(fresh_calls) >= 2


def test_open_new_tab_exact_session_registration_is_ready_while_page_loads(
    monkeypatch,
):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-loading",
                    "load_ready": False,
                    "status": "loading",
                }
            }

    driver = NewTabDriver(default="chrome:profile:7")
    _fresh_tab_ownership(monkeypatch)
    _install(
        monkeypatch,
        driver,
        [
            {
                "id": "chrome:profile:9",
                "url": "https://new.test/",
                "browser": "chrome",
                "generation": "generation-loading",
            }
        ],
    )

    result = S.open_new_tab("https://new.test/", timeout=1.0)

    assert result["ready"] is True
    assert result["status"] == "ok"
    assert result["load_status"] == "loading"
    assert result["owned"] is True
    assert result["owner_id"]


def test_extension_tab_create_ack_does_not_wait_for_page_load():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("async function createTabAck")
    end = source.index("\n\nasync function handleExtMessage", start)
    function_source = source[start:end]
    script = f"""
const chrome = {{
  tabs: {{
    create: async () => ({{
      id: 19,
      pendingUrl: 'https://slow.test/',
      url: '',
      title: '',
      windowId: 2,
      status: 'loading',
    }}),
    get: async () => new Promise(() => {{}}),
  }},
}};
async function scheduleNewTabGeneration(tabId) {{ return `generation-${{tabId}}`; }}
async function tabGenerationFor() {{ return null; }}
async function sendTabsUpdate() {{ return new Promise(() => {{}}); }}
{function_source}
Promise.race([
  createTabAck({{url: 'https://slow.test/', active: false}}),
  new Promise((_, reject) => setTimeout(() => reject(new Error('ack waited for load')), 100)),
]).then(result => process.stdout.write(JSON.stringify(result)))
  .catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    result = json.loads(completed.stdout)
    assert result["data"] == {
        "id": 19,
        "generation": "generation-19",
        "url": "https://slow.test/",
        "title": "",
        "windowId": 2,
        "status": "loading",
        "load_ready": False,
    }


def test_extension_tab_create_ack_reuses_generation_after_oncreated_finishes():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("async function createTabAck")
    end = source.index("\n\nasync function handleExtMessage", start)
    function_source = source[start:end]
    script = f"""
const chrome = {{ tabs: {{ create: async () => ({{
  id: 19, pendingUrl: 'https://slow.test/', url: '', title: '', windowId: 2,
  status: 'loading',
}}) }} }};
const generations = new Map([[19, 'generation-existing']]);
let scheduleCalls = 0;
async function tabGenerationFor(tabId) {{ return generations.get(tabId); }}
async function scheduleNewTabGeneration(tabId) {{
  scheduleCalls += 1;
  generations.set(tabId, `generation-replaced-${{scheduleCalls}}`);
  return generations.get(tabId);
}}
async function sendTabsUpdate() {{}}
{function_source}
createTabAck({{url: 'https://slow.test/'}}).then(result =>
  process.stdout.write(JSON.stringify({{result, scheduleCalls}}))
).catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    outcome = json.loads(completed.stdout)
    assert outcome["result"]["data"]["generation"] == "generation-existing"
    assert outcome["scheduleCalls"] == 0


def test_extension_tab_updates_publish_stable_lifecycle_generations():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "tabGenerationFor" in source
    assert "scheduleNewTabGeneration" in source
    assert "generation: await tabGenerationFor" in source


def test_extension_safe_close_rejects_generation_mismatch_before_remove():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("async function validateTabCloseGenerations")
    end = source.index("\n\n// --- Temporary, origin-scoped", start)
    function_source = source[start:end]
    script = f"""
const generations = new Map([[7, 'generation-live'], [8, 'generation-other']]);
async function tabGenerationFor(tabId) {{ return generations.get(tabId); }}
{function_source}
(async () => {{
  const ok = await validateTabCloseGenerations([7], {{'7': 'generation-live'}});
  const mismatch = await validateTabCloseGenerations([7], {{'7': 'generation-old'}});
  const mixed = await validateTabCloseGenerations(
    [7, 8], {{'7': 'generation-live', '8': 'generation-old'}}
  );
  process.stdout.write(JSON.stringify({{ok, mismatch, mixed}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    result = json.loads(completed.stdout)
    assert result["ok"] is None
    assert "generation changed" in result["mismatch"]
    assert "generation changed" in result["mixed"]


def test_extension_owned_close_atomically_reports_missing_and_closes_live_tabs():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("async function closeTabsWithGenerations")
    end = source.index("\n\n// --- Temporary, origin-scoped", start)
    function_source = source[start:end]
    script = f"""
const live = new Map([[8, {{id: 8}}]]);
const generations = new Map([[8, 'generation-live']]);
const removed = [];
const chrome = {{ tabs: {{
  get: async tabId => live.has(tabId) ? live.get(tabId) : Promise.reject(new Error('No tab')),
  remove: async tabIds => removed.push(...tabIds),
}} }};
async function tabGenerationFor(tabId) {{ return generations.get(tabId); }}
{function_source}
(async () => {{
  const result = await closeTabsWithGenerations(
    [7, 8], {{'7': 'generation-gone', '8': 'generation-live'}}
  );
  process.stdout.write(JSON.stringify({{result, removed}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    outcome = json.loads(completed.stdout)
    assert outcome["result"] == {"closed": [8], "alreadyGone": [7]}
    assert outcome["removed"] == [8]


def test_bridge_replaces_same_session_id_when_tab_generation_changes():
    driver = TMWebDriver.__new__(TMWebDriver)
    driver.sessions = {}
    driver.default_session_id = "chrome:profile:9"
    driver.latest_session_id = "chrome:profile:9"
    old_client = object()
    old = Session(
        "chrome:profile:9",
        {
            "url": "https://old.test/",
            "type": "ext_ws",
            "client_id": "chrome:profile",
            "browser": "chrome",
            "tab_id": 9,
            "generation": "generation-old",
        },
        old_client,
    )
    driver.sessions[old.id] = old
    new_client = object()

    driver._apply_extension_tabs(
        "chrome:profile",
        "chrome",
        [
            {
                "id": 9,
                "url": "https://new.test/",
                "title": "new",
                "generation": "generation-new",
            }
        ],
        new_client,
    )

    current = driver.sessions["chrome:profile:9"]
    assert current is not old
    assert old.is_active() is False
    assert current.info["generation"] == "generation-new"
    assert current.url == "https://new.test/"
    assert current.ws_client is new_client


def test_extension_manifest_is_2_1_2():
    manifest = json.loads((BACKGROUND.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "2.1.2"


def test_extension_pass2_final_build_is_observable():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert source.count("2026.08.12-pass2-final") == 2


def test_save_pdf_accepts_real_extension_ws_payload(tmp_path, monkeypatch):
    pdf_bytes = b"%PDF-1.7\nABM regression fixture\n%%EOF\n"
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    driver = _Driver([{"data": {"data": encoded}}])
    _install(monkeypatch, driver)
    target = tmp_path / "capture.pdf"

    result = S.save_pdf(str(target), session_id="chrome:profile:7")

    assert result["status"] == "success"
    assert result["size"] == len(pdf_bytes)
    assert target.read_bytes() == pdf_bytes
    assert driver.calls[0][0]["method"] == "Page.printToPDF"


def test_access_side_effect_tabs_are_flagged(monkeypatch):
    driver = _Driver()
    _install(
        monkeypatch,
        driver,
        _sessions(url="https://login.cloudflareaccess.com/cdn-cgi/access/verify-code/x"),
    )
    tab = S.list_tabs()["tabs"][0]
    assert tab["automation_attention"] == "authentication_required"
    assert "hint" in tab


def test_background_declares_bounded_debugger_command_and_forced_invalidation():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "sendDebuggerCommandWithTimeout" in source
    assert "forceInvalidateDebuggerAttachment" in source
    handle_cdp = source[
        source.index("async function handleCDP") : source.index(
            "// Filter out chrome://", source.index("async function handleCDP")
        )
    ]
    assert "sendDebuggerCommandWithTimeout" in handle_cdp
    assert "chrome.debugger.sendCommand" not in handle_cdp


def test_debugger_watchdog_detaches_clears_lease_and_allows_reattach():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\n\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveCommands = false;
const chrome = {{ debugger: {{
  attach() {{ attachCalls += 1; return Promise.resolve(); }},
  detach() {{ detachCalls += 1; return Promise.resolve(); }},
  sendCommand() {{
    return resolveCommands ? Promise.resolve({{ value: 2 }}) : new Promise(() => {{}});
  }},
}} }};
eval(source.slice(start, end));
(async () => {{
  const first = await attachAbmDebugger({{ tabId: 42 }});
  let failure = null;
  try {{
    await sendDebuggerCommandWithTimeout(first, 'Runtime.evaluate', {{}}, 10);
  }} catch (error) {{
    failure = {{ code: error.code, timeoutMs: error.timeoutMs }};
  }}
  const afterTimeout = {{
    detachCalls,
    tracked: debuggerAttachments.size,
    pending: first.attachment.pendingCommands.size,
  }};
  resolveCommands = true;
  const second = await attachAbmDebugger({{ tabId: 42 }});
  const result = await sendDebuggerCommandWithTimeout(
    second, 'Runtime.evaluate', {{}}, 100,
  );
  await detachAbmDebugger(second);
  process.stdout.write(JSON.stringify({{
    failure, afterTimeout, attachCalls, detachCalls, result,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["failure"] == {"code": "cdp_timeout", "timeoutMs": 100}
    assert outcome["afterTimeout"] == {
        "detachCalls": 1,
        "tracked": 0,
        "pending": 0,
    }
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2
    assert outcome["result"] == {"value": 2}


def test_debugger_conflict_recovery_preserves_concurrent_lease_refs():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\n\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveRecoveryDetach;
let signalRecoveryDetach;
const recoveryDetachStarted = new Promise(resolve => {{ signalRecoveryDetach = resolve; }});
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    if (attachCalls === 1) return Promise.reject(new Error('Another debugger is already attached'));
    return Promise.resolve();
  }},
  detach() {{
    detachCalls += 1;
    if (detachCalls === 1) {{
      signalRecoveryDetach();
      return new Promise(resolve => {{ resolveRecoveryDetach = resolve; }});
    }}
    return Promise.resolve();
  }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  const firstPromise = attachAbmDebugger({{ tabId: 42 }});
  const secondPromise = attachAbmDebugger({{ tabId: 42 }});
  await recoveryDetachStarted;
  const thirdPromise = attachAbmDebugger({{ tabId: 42 }});
  resolveRecoveryDetach();
  const [first, second, third] = await Promise.all([
    firstPromise, secondPromise, thirdPromise,
  ]);
  const afterRecovery = {{
    refs: first.attachment.refs,
    attached: first.attachment.attached,
    tracked: debuggerAttachments.size,
    sameAttachment: first.attachment === second.attachment && second.attachment === third.attachment,
  }};
  await detachAbmDebugger(first);
  const afterFirstRelease = {{
    refs: third.attachment.refs,
    attached: third.attachment.attached,
    tracked: debuggerAttachments.size,
    detachCalls,
  }};
  await detachAbmDebugger(second);
  const afterSecondRelease = {{
    refs: third.attachment.refs,
    attached: third.attachment.attached,
    tracked: debuggerAttachments.size,
    detachCalls,
  }};
  await detachAbmDebugger(third);
  process.stdout.write(JSON.stringify({{
    attachCalls,
    detachCalls,
    afterRecovery,
    afterFirstRelease,
    afterSecondRelease,
    trackedAfterAll: debuggerAttachments.size,
    recoveriesAfterAll: debuggerRecoveryPromises.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["afterRecovery"] == {
        "refs": 3,
        "attached": True,
        "tracked": 1,
        "sameAttachment": True,
    }
    assert outcome["afterFirstRelease"] == {
        "refs": 2,
        "attached": True,
        "tracked": 1,
        "detachCalls": 1,
    }
    assert outcome["afterSecondRelease"] == {
        "refs": 1,
        "attached": True,
        "tracked": 1,
        "detachCalls": 1,
    }
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2
    assert outcome["trackedAfterAll"] == 0
    assert outcome["recoveriesAfterAll"] == 0


def test_external_debugger_conflict_preserves_original_error_and_cleans_state():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\n\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    return Promise.reject(new Error('Another debugger is already attached'));
  }},
  detach() {{
    detachCalls += 1;
    return Promise.reject(new Error('Debugger is not attached to the tab'));
  }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  let failure = null;
  try {{
    await attachAbmDebugger({{ tabId: 42 }});
  }} catch (error) {{
    failure = {{ message: error.message, code: debuggerFailureCode(error) }};
  }}
  process.stdout.write(JSON.stringify({{
    failure,
    attachCalls,
    detachCalls,
    tracked: debuggerAttachments.size,
    recoveries: debuggerRecoveryPromises.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["failure"] == {
        "message": "Another debugger is already attached",
        "code": "debugger_conflict",
    }
    assert outcome["attachCalls"] == 1
    assert outcome["detachCalls"] == 1
    assert outcome["tracked"] == 0
    assert outcome["recoveries"] == 0


def test_stopping_network_cancels_only_its_pending_body_watchdog():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const captureStart = source.indexOf('function boundedCaptureInteger');
const captureEnd = source.indexOf('\\n\\nfunction handleDebuggerEvent', captureStart);
const debuggerStart = source.indexOf('function debuggerTargetKey');
const debuggerEnd = source.indexOf('\\n\\nasync function handleProtocolDialog', debuggerStart);
if (captureStart < 0 || captureEnd < 0 || debuggerStart < 0 || debuggerEnd < 0) {{
  throw new Error('capture/debugger helpers not found');
}}
const networkCaptures = new Map();
const consoleCaptures = new Map();
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
const chrome = {{ debugger: {{
  attach() {{ attachCalls += 1; return Promise.resolve(); }},
  detach() {{
    detachCalls += 1;
    markCaptureTabInvalidated(42, 'debugger detached');
    return Promise.resolve();
  }},
  sendCommand(target, method) {{
    if (method === 'Network.getResponseBody') return new Promise(() => {{}});
    return Promise.resolve({{}});
  }},
}} }};
eval(source.slice(debuggerStart, debuggerEnd));
eval(source.slice(captureStart, captureEnd));
(async () => {{
  await handleNetworkCaptureCommand({{
    method: 'start', tabId: 42, includeBodies: true, bodyTimeoutMs: 100,
  }}, {{}});
  await handleConsoleCaptureCommand({{ method: 'start', tabId: 42 }}, {{}});
  const sharedAttachment = debuggerAttachments.get('tab:42');
  handleNetworkCaptureEvent(42, 'Network.requestWillBeSent', {{
    requestId: 'req-1', request: {{ url: 'https://example.test/', method: 'GET' }},
  }});
  handleNetworkCaptureEvent(42, 'Network.loadingFinished', {{
    requestId: 'req-1', encodedDataLength: 1,
  }});
  const beforeStop = {{
    refs: sharedAttachment.refs,
    pending: sharedAttachment.pendingCommands.size,
  }};
  const networkStop = await handleNetworkCaptureCommand({{ method: 'stop', tabId: 42 }}, {{}});
  await new Promise(resolve => setTimeout(resolve, 160));
  const consoleCapture = consoleCaptures.get(42);
  const afterBodyDeadline = {{
    consoleActive: consoleCapture?.active,
    refs: sharedAttachment.refs,
    attached: sharedAttachment.attached,
    tracked: debuggerAttachments.get('tab:42') === sharedAttachment,
    pending: sharedAttachment.pendingCommands.size,
    pendingTimers: [...sharedAttachment.pendingCommands].filter(command => command.timer !== null).length,
    detachCalls,
  }};
  const consoleStop = await handleConsoleCaptureCommand({{ method: 'stop', tabId: 42 }}, {{}});
  await handleNetworkCaptureCommand({{
    method: 'start', tabId: 42, includeBodies: false,
  }}, {{}});
  const soloNetworkStop = await handleNetworkCaptureCommand({{ method: 'stop', tabId: 42 }}, {{}});
  process.stdout.write(JSON.stringify({{
    attachCalls,
    detachCalls,
    beforeStop,
    networkStop: networkStop.data.status,
    networkBodyError: networkStop.data.requests[0]?.body_error,
    afterBodyDeadline,
    consoleStop: consoleStop.data.status,
    soloNetworkStop: soloNetworkStop.data,
    trackedAfterAll: debuggerAttachments.size,
    pendingAfterAll: sharedAttachment.pendingCommands.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["beforeStop"] == {"refs": 2, "pending": 1}
    assert outcome["networkStop"] == "stopped"
    assert "owning lease released" in outcome["networkBodyError"]
    assert outcome["afterBodyDeadline"] == {
        "consoleActive": True,
        "refs": 1,
        "attached": True,
        "tracked": True,
        "pending": 0,
        "pendingTimers": 0,
        "detachCalls": 0,
    }
    assert outcome["consoleStop"] == "stopped"
    assert outcome["soloNetworkStop"]["status"] == "stopped"
    assert "error" not in outcome["soloNetworkStop"]
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2
    assert outcome["trackedAfterAll"] == 0
    assert outcome["pendingAfterAll"] == 0
