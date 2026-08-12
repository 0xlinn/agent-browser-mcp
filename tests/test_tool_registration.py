"""The MCP surface: what the agent can see and call.

Two things are easy to break by accident and invisible until runtime:
  - the thread-offload wrapper reassigns mcp.tool, so a mistake there makes
    pydantic fail to build a tool's schema, or leaves a sync function on the
    event loop where a slow call blocks the whole server;
  - in-process callers (and the live tests) call server.scan_page directly, so
    the module-level names must stay plain sync functions.
"""
import asyncio
import inspect

import pytest

from agent_browser_mcp import server as S

EXPECTED = {
    # discovery / diagnostics
    "get_setup_status", "get_automation_profile", "set_automation_profile",
    "list_tabs", "list_all_tabs", "extension_path",
    # tabs
    "open_url", "open_new_tab", "close_tabs", "switch_tab", "activate_tab",
    # extensions
    "list_extensions", "set_extension_enabled", "uninstall_extension",
    "get_bookmarks", "create_bookmark", "remove_bookmark", "call_extension",
    "download_file",
    "network_capture_start", "network_capture_stop", "console_capture_start",
    "get_console_messages", "console_capture_stop",
    "save_pdf",
    # reading
    "scan_page",
    # waiting / scrolling
    "wait_for", "scroll_page",
    # execution
    "execute_js", "handle_dialog", "resolve_leave_dialog", "cdp_command",
    "cdp_batch", "debugger_targets",
    # data
    "get_cookies", "capture_page_screenshot", "capture_desktop_screenshot",
    "upload_files",
    # physical input
    "mouse_move", "mouse_click", "mouse_drag", "type_text", "hotkey",
    "pointer_info",
    # background page input
    "page_click", "page_type", "page_press", "page_drag",
    # cookies / storage
    "set_cookies", "delete_cookies", "storage_get", "storage_set",
    # temporary, origin-scoped site permissions
    "set_site_permission", "reset_site_permissions",
    # navigation wait
    "wait_for_url",
}

NEW_IN_THIS_ROUND = {
    "wait_for",
    "scroll_page",
    "activate_tab",
    "upload_files",
    "get_automation_profile",
    "set_automation_profile",
    "resolve_leave_dialog",
    "uninstall_extension",
    "get_bookmarks",
    "create_bookmark",
    "remove_bookmark",
    "call_extension",
    "network_capture_start",
    "network_capture_stop",
    "console_capture_start",
    "get_console_messages",
    "console_capture_stop",
    "save_pdf",
}


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(S.mcp.list_tools())


@pytest.fixture(scope="module")
def by_name(tools):
    return {t.name: t for t in tools}


def test_expected_tool_set(by_name):
    assert set(by_name) == EXPECTED


def test_new_tools_are_registered(by_name):
    assert NEW_IN_THIS_ROUND <= set(by_name)


def test_every_tool_has_a_description(by_name):
    missing = [n for n, t in by_name.items() if not (t.description or "").strip()]
    assert not missing


def test_all_registered_tools_are_coroutines(by_name):
    """A sync function here would run on the event loop and block the server."""
    sync = []
    for name in by_name:
        fn = S.mcp._tool_manager.get_tool(name).fn
        if not inspect.iscoroutinefunction(fn):
            sync.append(name)
    assert not sync


def test_module_level_functions_stay_sync():
    """The decorator returns the original function so in-process calls work."""
    for name in ("scan_page", "wait_for", "scroll_page", "list_tabs", "open_url"):
        fn = getattr(S, name)
        assert callable(fn)
        assert not inspect.iscoroutinefunction(fn), name


def test_schemas_resolve_annotations(by_name):
    """`from __future__ import annotations` makes these strings; if the wrapper
    loses the resolved signature, pydantic raises instead of building a schema."""
    for name, tool in by_name.items():
        schema = tool.inputSchema
        assert schema.get("type") == "object", name
        assert "properties" in schema, name


class TestNewToolSchemas:
    def test_wait_for_params(self, by_name):
        props = by_name["wait_for"].inputSchema["properties"]
        assert {"selector", "text", "url_pattern", "js", "timeout", "gone",
                "session_id"} <= set(props)

    def test_scroll_page_params(self, by_name):
        props = by_name["scroll_page"].inputSchema["properties"]
        assert {"to", "session_id", "timeout"} <= set(props)

    def test_activate_tab_params(self, by_name):
        props = by_name["activate_tab"].inputSchema["properties"]
        assert "session_id" in props

    def test_upload_files_params(self, by_name):
        props = by_name["upload_files"].inputSchema["properties"]
        assert {"selector", "paths", "session_id", "timeout"} <= set(props)

    def test_page_input_params(self, by_name):
        assert {"selector", "x", "y", "offset_x", "offset_y", "button", "clicks",
                "session_id", "timeout"} <= set(
                    by_name["page_click"].inputSchema["properties"])
        assert {"text", "selector", "clear", "submit_key", "session_id", "timeout"} <= set(
            by_name["page_type"].inputSchema["properties"])
        assert {"keys_csv", "session_id", "timeout"} <= set(
            by_name["page_press"].inputSchema["properties"])
        assert {"x1", "y1", "x2", "y2", "duration", "button", "session_id",
                "timeout"} <= set(by_name["page_drag"].inputSchema["properties"])

    def test_dialog_policy_params(self, by_name):
        open_url = by_name["open_url"].inputSchema["properties"]
        execute_js = by_name["execute_js"].inputSchema["properties"]
        handle = by_name["handle_dialog"].inputSchema["properties"]
        assert open_url["beforeunload"]["default"] == "dismiss"
        assert execute_js["dialog_policy"]["default"] == "dismiss"
        assert {"action", "prompt_text", "session_id"} <= set(handle)

    def test_automation_profile_and_leave_resolution_params(self, by_name):
        assert set(by_name["get_automation_profile"].inputSchema["properties"]) == set()
        assert "mode" in by_name["set_automation_profile"].inputSchema["properties"]
        assert "session_id" in by_name["resolve_leave_dialog"].inputSchema["properties"]
        assert "ctx" not in by_name["resolve_leave_dialog"].inputSchema["properties"]

    def test_tab_ownership_params_are_exposed_with_safe_close_default(self, by_name):
        opened = by_name["open_new_tab"].inputSchema["properties"]
        closed = by_name["close_tabs"].inputSchema["properties"]
        assert "owner_id" in opened
        assert {"owner_id", "only_if_agent_owned"} <= set(closed)
        assert closed["only_if_agent_owned"]["default"] is True

    def test_extension_and_bookmark_params(self, by_name):
        assert {"extension_id", "show_confirm_dialog", "session_id"} <= set(
            by_name["uninstall_extension"].inputSchema["properties"]
        )
        assert {"title", "url", "parent_id", "session_id"} <= set(
            by_name["create_bookmark"].inputSchema["properties"]
        )
        assert {"bookmark_id", "recursive", "session_id"} <= set(
            by_name["remove_bookmark"].inputSchema["properties"]
        )
        assert {"extension_id", "message_json", "session_id"} <= set(
            by_name["call_extension"].inputSchema["properties"]
        )
        assert {"url", "filename", "directory", "wait", "timeout", "session_id", "overwrite"} <= set(
            by_name["download_file"].inputSchema["properties"]
        )
        assert by_name["download_file"].inputSchema["properties"]["overwrite"]["default"] is False

    def test_capture_params(self, by_name):
        assert {
            "session_id", "include_bodies", "max_entries", "max_body_bytes",
            "body_timeout", "timeout",
        } <= set(by_name["network_capture_start"].inputSchema["properties"])
        assert {"session_id", "max_entries", "timeout"} <= set(
            by_name["console_capture_start"].inputSchema["properties"]
        )
        assert {"session_id", "offset", "max_items", "clear", "timeout"} <= set(
            by_name["get_console_messages"].inputSchema["properties"]
        )

    def test_save_pdf_params(self, by_name):
        assert {
            "save_path", "session_id", "landscape", "print_background",
            "prefer_css_page_size", "scale", "page_ranges", "timeout",
        } <= set(by_name["save_pdf"].inputSchema["properties"])

    def test_physical_input_can_activate_a_tab(self, by_name):
        """Screen-coordinate input lands on whatever is visible, so the tools
        that move the mouse or type must be able to raise the target first."""
        for name in ("mouse_click", "type_text"):
            props = by_name[name].inputSchema["properties"]
            assert "activate_session" in props, name

    def test_physical_input_raises_the_tab_by_default(self, by_name):
        """Raising must be the DEFAULT, not opt-in. When it was opt-in, an agent
        doing switch_tab + mouse_click clicked the previously visible tab, and
        nothing reported it: the coordinates are valid and pyautogui says ok."""
        for name in ("mouse_click", "type_text"):
            props = by_name[name].inputSchema["properties"]
            assert props["activate_session"]["default"] == "current", name

    def test_physical_tools_hide_injected_context(self, by_name):
        for name in ("mouse_move", "mouse_click", "mouse_drag", "type_text", "hotkey"):
            props = by_name[name].inputSchema["properties"]
            assert "ctx" not in props, name
            assert "ctx" not in by_name[name].inputSchema.get("required", []), name

    def test_physical_descriptions_warn_about_target_and_foreground_approval(self, by_name):
        for name in ("mouse_click", "type_text"):
            description = by_name[name].description.lower()
            assert "session_id" in description, name
            assert "prefer" in description or "preferred" in description, name
            assert "approval" in description, name
            assert "foreground" in description, name

    def test_switch_tab_is_background_by_default(self, by_name):
        props = by_name["switch_tab"].inputSchema["properties"]
        assert props["activate"]["default"] is False

    def test_site_permission_params(self, by_name):
        assert {"permission", "setting", "origin", "duration_seconds", "session_id"} <= set(
            by_name["set_site_permission"].inputSchema["properties"])
        assert {"origin", "permission", "session_id"} <= set(
            by_name["reset_site_permissions"].inputSchema["properties"])


class TestArgumentValidation:
    """wait_for's conditions are mutually exclusive; check it refuses early
    instead of silently waiting on the wrong thing. No browser needed."""

    def test_no_condition_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            S.wait_for(timeout=1)

    def test_two_conditions_raise(self):
        with pytest.raises(ValueError, match="exactly one"):
            S.wait_for(selector="body", text="hello", timeout=1)

    def test_all_four_conditions_raise(self):
        with pytest.raises(ValueError, match="exactly one"):
            S.wait_for(selector="a", text="b", url_pattern="c", js="true", timeout=1)


class TestUploadValidation:
    def test_missing_file_is_rejected_before_touching_the_browser(self):
        with pytest.raises(RuntimeError, match="not found"):
            S.upload_files("input[type=file]", "D:/nope/definitely-missing.txt")

    def test_missing_file_in_a_list_is_rejected(self):
        with pytest.raises(RuntimeError, match="not found"):
            S.upload_files("input", ["D:/nope/a.txt", "D:/nope/b.txt"])
