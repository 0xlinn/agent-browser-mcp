"""Shared fixtures.

The offline layer imports agent_browser_mcp directly and never touches a
browser. The live layer needs a running bridge daemon plus the extension
connected, so it is marked `live` and skipped by default (see pyproject).
"""
from __future__ import annotations

import random
import socket
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _free_port_base() -> int:
    """A port p where p, p+1 and p+2 are all free.

    TMWebDriver takes three consecutive ports (WS, /link, host lock), so a
    single bind test is not enough. Random high ports, retried, rather than
    anything near 18765: a test must never collide with the daemon the user has
    running, let alone bind over it.
    """
    import socket as _s

    for _ in range(50):
        base = random.randint(31000, 60000)
        socks = []
        try:
            for offset in range(3):
                sock = _s.socket()
                # Windows 上普通 bind 会被 SO_REUSEADDR 的双重绑定骗过（探测
                # "成功"，实际端口已被泄漏的旧测试桥占着，请求随机落到旧桥）。
                # 独占探测才回答"这端口真没人用"。
                try:
                    sock.setsockopt(_s.SOL_SOCKET, _s.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass  # 非 Windows 无此选项
                sock.bind(("127.0.0.1", base + offset))
                socks.append(sock)
        except OSError:
            continue
        finally:
            for sock in socks:
                sock.close()
        if len(socks) == 3:
            return base
    pytest.skip("no free port triple for a test bridge")


class _TestBridge:
    def __init__(self, driver, base):
        self.driver = driver
        self.base = base
        self.port = base + 1     # the /link HTTP port


def _spawn_test_bridge(token, monkeypatch):
    """A real in-process TMWebDriver host on throwaway ports.

    In-process, not a subprocess, so nothing can outlive the test run and start
    competing with the user's daemon for the real ports.
    """
    import time

    from agent_browser_mcp.tmwebdriver import TMWebDriver, TOKEN_AUTH_ENV, TOKEN_ENV

    if token:
        monkeypatch.delenv(TOKEN_AUTH_ENV, raising=False)
        monkeypatch.setenv(TOKEN_ENV, token)
    else:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        monkeypatch.setenv(TOKEN_AUTH_ENV, "off")
    base = _free_port_base()
    d = TMWebDriver.__new__(TMWebDriver)
    d.host, d.port = "127.0.0.1", base
    d.sessions, d.results, d.acks = {}, {}, {}
    d.default_session_id = d.latest_session_id = None
    d.last_ext_seen = None
    d.client_last_seen, d.ext_clients = {}, {}
    d.is_remote = False
    # Only the HTTP half: these tests are about who may POST to /link, and
    # binding the WS port too would just be another listener to clean up.
    d.start_http_server()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", base + 1)) == 0:
                break
        time.sleep(0.05)
    else:
        pytest.skip(f"test bridge never bound 127.0.0.1:{base + 1}")
    return _TestBridge(d, base)


@pytest.fixture
def link_bridge_auth(monkeypatch):
    """A test bridge that requires a token (the value tests/test_link_auth use)."""
    return _spawn_test_bridge("s3cret-token", monkeypatch)


@pytest.fixture
def link_bridge_open(monkeypatch):
    """A test bridge with authentication explicitly disabled."""
    return _spawn_test_bridge(None, monkeypatch)


@pytest.fixture(scope="session")
def driver():
    """A TMWebDriver talking to an already-running bridge, or skip."""
    from agent_browser_mcp.tmwebdriver import TMWebDriver

    d = TMWebDriver()
    try:
        sessions = d.get_all_sessions()
    except Exception as e:  # bridge not up
        pytest.skip(f"bridge unreachable: {e}")
    if not sessions:
        pytest.skip("bridge is up but no tab is connected")
    return d


@pytest.fixture(scope="session")
def scratch_session(driver):
    """One throwaway tab, reused by every live test, closed at the end.

    Opening a tab per test would litter the user's browser; every live test
    navigates this one instead.
    """
    from agent_browser_mcp import server as S

    import time

    result = S.open_new_tab("https://example.com/")
    tab_id = result["result"]["data"]["id"]
    owner_id = result.get("owner_id")

    sid = None
    for _ in range(20):
        for s in driver.get_all_sessions():
            if str(s["id"]).endswith(f":{tab_id}"):
                sid = s["id"]
                break
        if sid:
            break
        time.sleep(0.5)
    if not sid:
        pytest.skip("scratch tab never registered a session")

    # server.active_sessions() is cached, and switch_session() looks the target
    # up in that cache. Without this the brand-new tab is "not found" even
    # though the bridge already knows about it.
    S.invalidate_sessions_cache()
    for _ in range(10):
        if any(str(s.get("id")) == sid for s in S.active_sessions(fresh=True)):
            break
        time.sleep(0.5)

    yield sid

    try:
        S.close_tabs(
            tab_id,
            session_id=sid,
            owner_id=owner_id,
        )
    except Exception:
        pass
