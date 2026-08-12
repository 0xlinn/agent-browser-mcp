"""Screenshot results must be useful without lying about model vision."""
from __future__ import annotations

import asyncio
import base64
import json

from mcp.types import CallToolResult, ImageContent, TextContent

from agent_browser_mcp import server as S


PNG_BYTES = b"\x89PNG\r\n\x1a\nsmall-test-image"
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")


class FakeScreenshotDriver:
    def __init__(self, default_session_id="chrome:profile:42"):
        self.default_session_id = default_session_id
        self.calls = []

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.calls.append((payload, client_id, timeout))
        return {"data": {"data": PNG_BASE64}}


def _install_capture(monkeypatch, driver=None, sessions=None):
    driver = driver or FakeScreenshotDriver()
    sessions = sessions or [
        {"id": "chrome:profile:42", "url": "https://example.test/"}
    ]
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(
        S, "active_sessions", lambda timeout=None, fresh=False: sessions
    )
    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("screenshots must not use the page/content route")
        ),
    )
    return driver


def test_screenshot_attaches_image_even_when_saved(monkeypatch, tmp_path):
    _install_capture(monkeypatch)
    target = tmp_path / "shot.png"

    result = S.capture_page_screenshot(save_path=str(target))

    assert isinstance(result, CallToolResult)
    assert target.read_bytes() == PNG_BYTES
    assert result.structuredContent["saved_to"] == str(target.resolve())
    assert result.structuredContent["image_attached"] is True
    assert "base64" not in result.structuredContent
    assert isinstance(result.content[0], TextContent)
    assert isinstance(result.content[1], ImageContent)
    assert base64.b64decode(result.content[1].data) == PNG_BYTES
    text = json.loads(result.content[0].text)
    assert "does not support images" in text["model_note"]


def test_base64_is_only_returned_when_explicit(monkeypatch):
    _install_capture(monkeypatch)

    default = S.capture_page_screenshot()
    explicit = S.capture_page_screenshot(return_base64=True)

    assert "base64" not in default.structuredContent
    assert explicit.structuredContent["base64"] == PNG_BASE64
    assert PNG_BASE64 not in explicit.content[0].text


def test_fastmcp_preserves_text_image_and_structured_metadata(monkeypatch, tmp_path):
    _install_capture(monkeypatch)

    result = asyncio.run(S.mcp.call_tool(
        "capture_page_screenshot", {"save_path": str(tmp_path / "mcp.png")}))

    assert isinstance(result, CallToolResult)
    assert [block.type for block in result.content] == ["text", "image"]
    assert result.structuredContent["image_attached"] is True


def test_directed_screenshot_uses_service_worker_route_and_restores_default(monkeypatch):
    driver = FakeScreenshotDriver(default_session_id="edge:other:7")
    sid = "chrome:profile:42"
    _install_capture(
        monkeypatch,
        driver=driver,
        sessions=[{"id": sid, "url": "https://example.test/"}],
    )

    S.capture_page_screenshot(session_id=sid)

    assert driver.calls == [
        (
            {
                "cmd": "cdp",
                "method": "Page.captureScreenshot",
                "params": {"format": "png"},
                "tabId": 42,
            },
            "chrome:profile",
            20.0,
        )
    ]
    assert driver.default_session_id == "edge:other:7"


def test_directed_screenshot_rejects_dead_or_mismatched_target(monkeypatch):
    driver = FakeScreenshotDriver(default_session_id="edge:other:7")
    _install_capture(
        monkeypatch,
        driver=driver,
        sessions=[{"id": "chrome:profile:99", "url": "https://live.test/"}],
    )

    for kwargs, message in [
        ({"session_id": "chrome:profile:42"}, "not found"),
        ({"session_id": "chrome:profile:99", "tab_id": 42}, "does not match"),
    ]:
        try:
            S.capture_page_screenshot(**kwargs)
        except (RuntimeError, ValueError) as exc:
            assert message in str(exc)
        else:
            raise AssertionError("invalid directed screenshot target was accepted")

    assert driver.calls == []
    assert driver.default_session_id == "edge:other:7"


def test_default_screenshot_recovers_a_stale_implicit_session(monkeypatch):
    driver = FakeScreenshotDriver(default_session_id="chrome:profile:1")
    _install_capture(
        monkeypatch,
        driver=driver,
        sessions=[{"id": "chrome:profile:42", "url": "https://live.test/"}],
    )

    S.capture_page_screenshot()

    assert driver.calls[0][0]["tabId"] == 42
    assert driver.calls[0][1] == "chrome:profile"
    assert driver.default_session_id == "chrome:profile:42"


def test_empty_documents_are_guarded_before_dom_queries():
    assert "if (!domCopy)" in S.simphtml.js_optHTML
    assert "if (!root) return [];" in S.simphtml.js_findMainList
