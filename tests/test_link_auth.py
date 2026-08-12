"""The /link HTTP port is a command channel; who may speak to it.

The WS port keeps web pages out with an Origin prefix check, which works there
because a page cannot forge chrome-extension://. /link has no such handle: any
local process can POST an execute_js and run arbitrary JS in the user's
logged-in tabs. A shared token closes that, and is opt-in so existing setups
without one keep working.

Every test here binds its OWN ports (see conftest's link_bridge fixture) and
never touches the bridge daemon the user has running.
"""
from __future__ import annotations

import pytest
import requests

from agent_browser_mcp import tmwebdriver as T

TOKEN = "s3cret-token"


# --- the pure header parser ------------------------------------------------

@pytest.mark.parametrize("headers,expected", [
    ({"Authorization": f"Bearer {TOKEN}"}, TOKEN),
    ({"Authorization": f"bearer {TOKEN}"}, TOKEN),      # scheme is case-insensitive
    ({"Authorization": f"BEARER {TOKEN}"}, TOKEN),
    ({"X-Bridge-Token": TOKEN}, TOKEN),
    ({"Authorization": f"Bearer  {TOKEN} "}, TOKEN),    # padding is stripped
    ({}, ""),
    ({"Authorization": TOKEN}, ""),                     # no scheme is not a token
    ({"Authorization": "Basic abc"}, ""),
    ({"Authorization": "Bearer"}, ""),
    ({"Authorization": "Bearer   "}, ""),
])
def test_header_token(headers, expected):
    assert T.header_token(headers) == expected


def test_authorization_wins_over_x_bridge_token():
    got = T.header_token({"Authorization": f"Bearer {TOKEN}", "X-Bridge-Token": "other"})
    assert got == TOKEN


def test_no_token_configured_lets_everything_through(monkeypatch):
    """Back-compat: without the env var the bridge must behave as it always did."""
    monkeypatch.delenv(T.TOKEN_ENV, raising=False)
    T.require_link_token({})              # must not raise
    T.require_link_token({"Authorization": "Bearer nonsense"})


def test_configured_token_rejects_a_missing_header(monkeypatch):
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    with pytest.raises(Exception) as exc:
        T.require_link_token({})
    assert getattr(exc.value, "status_code", None) == 401


def test_configured_token_rejects_a_wrong_header(monkeypatch):
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    with pytest.raises(Exception) as exc:
        T.require_link_token({"Authorization": "Bearer wrong"})
    assert getattr(exc.value, "status_code", None) == 401


def test_configured_token_accepts_either_header(monkeypatch):
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    T.require_link_token({"Authorization": f"Bearer {TOKEN}"})
    T.require_link_token({"X-Bridge-Token": TOKEN})


def test_a_prefix_of_the_token_is_not_enough(monkeypatch):
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    for bad in (TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
        with pytest.raises(Exception):
            T.require_link_token({"X-Bridge-Token": bad})


def test_whitespace_only_env_counts_as_unset(monkeypatch):
    """Otherwise `set VAR= ` would lock out every client with an unguessable
    secret nobody can reproduce."""
    monkeypatch.setenv(T.TOKEN_ENV, "   ")
    assert T.bridge_token() == ""
    T.require_link_token({})


# --- end to end over real HTTP, on a throwaway port ------------------------

def _post(port, body, headers=None, timeout=5):
    return requests.post(f"http://127.0.0.1:{port}/link", json=body,
                         headers=headers or {}, timeout=timeout)


class TestUnauthenticatedBridge:
    """No token configured: the historical behaviour, unchanged."""

    def test_bare_post_is_served(self, link_bridge_open):
        r = _post(link_bridge_open.port, {"cmd": "get_all_sessions"})
        assert r.status_code == 200
        assert r.json()["r"] == []


class TestUnknownCommandIsAnError:
    """A /link cmd that this bridge does not know must answer with a
    structured error, never the bare string "ok" that looks like success."""

    def test_unknown_cmd_reports_error_not_ok(self, link_bridge_open):
        r = _post(link_bridge_open.port, {"cmd": "open_new_tab", "url": "https://x"})
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict) and "r" in body
        assert "error" in body["r"]
        assert "unknown cmd" in body["r"]["error"]

    def test_ext_cmd_is_not_confused_with_top_level_payload(self, link_bridge_open):
        """/link expects {"cmd":"ext_cmd","payload":{...}}. A caller that puts the
        extension payload at the top level ({"cmd":"tabs",...}) is a protocol
        mistake and must be told, not silently acknowledged."""
        r = _post(link_bridge_open.port, {"cmd": "tabs", "method": "create", "url": "https://x"})
        assert r.status_code == 200
        body = r.json()
        assert "unknown cmd" in body["r"]["error"]

    def test_non_object_body_is_an_error(self, link_bridge_open):
        r = requests.post(f"http://127.0.0.1:{link_bridge_open.port}/link",
                          data="not json", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict) and "error" in body.get("r", {})


class TestAuthenticatedBridge:
    def test_missing_token_is_401(self, link_bridge_auth):
        r = _post(link_bridge_auth.port, {"cmd": "get_all_sessions"})
        assert r.status_code == 401
        assert "token" in r.text.lower()

    def test_wrong_token_is_401(self, link_bridge_auth):
        r = _post(link_bridge_auth.port, {"cmd": "get_all_sessions"},
                  {"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_bearer_token_is_200(self, link_bridge_auth):
        r = _post(link_bridge_auth.port, {"cmd": "get_all_sessions"},
                  {"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        assert r.json()["r"] == []

    def test_x_bridge_token_is_200(self, link_bridge_auth):
        r = _post(link_bridge_auth.port, {"cmd": "get_all_sessions"},
                  {"X-Bridge-Token": TOKEN})
        assert r.status_code == 200

    def test_execute_js_is_refused_before_it_reaches_a_tab(self, link_bridge_auth):
        """The actual hole: an unauthenticated execute_js used to run arbitrary
        JS in the user's logged-in Chrome. It must be rejected at the door, not
        answered with a bridge-level error that proves it got parsed."""
        r = _post(link_bridge_auth.port,
                  {"cmd": "execute_js", "code": "document.cookie", "sessionId": None})
        assert r.status_code == 401

    def test_diagnose_is_guarded_too(self, link_bridge_auth):
        """Diagnostics leak the tab inventory, so they are not a free route."""
        r = _post(link_bridge_auth.port, {"cmd": "diagnose"})
        assert r.status_code == 401

    def test_result_channel_is_guarded_too(self, link_bridge_auth):
        """/api/result can inject fake execution results into the daemon. A
        token-configured bridge must demand the token there as well, or a local
        process could forge results without ever touching /link."""
        r = _post(link_bridge_auth.port, {"cmd": "longpoll_marker"})
        # Unknown cmd is not a valid route here; use the actual endpoints.
        r2 = requests.post(
            f"http://127.0.0.1:{link_bridge_auth.port}/api/result",
            json={"type": "result", "id": "forged", "result": "x"},
            timeout=5)
        assert r2.status_code == 401
        r3 = requests.post(
            f"http://127.0.0.1:{link_bridge_auth.port}/api/longpoll",
            json={"sessionId": "attacker:1"},
            timeout=5)
        assert r3.status_code == 401

    def test_result_channel_still_open_without_token(self, link_bridge_open):
        """Backwards compatibility: with no token configured the result
        channel behaves exactly as before."""
        r = requests.post(
            f"http://127.0.0.1:{link_bridge_open.port}/api/result",
            json={"type": "result", "id": "x", "result": "y"},
            timeout=5)
        assert r.status_code == 200

    def test_result_channel_accepts_token(self, link_bridge_auth):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        r = requests.post(
            f"http://127.0.0.1:{link_bridge_auth.port}/api/result",
            json={"type": "result", "id": "ok-id", "result": "y"},
            headers=headers, timeout=5)
        assert r.status_code == 200


class TestRemoteClientCarriesTheToken:
    def test_remote_cmd_authenticates_itself(self, link_bridge_auth, monkeypatch):
        """A remote MCP instance reads the same env var, so a token-protected
        bridge stays usable without any per-client configuration."""
        monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
        client = _remote_client(link_bridge_auth.port)
        assert client._remote_cmd({"cmd": "get_all_sessions"})["r"] == []

    def test_remote_cmd_without_the_token_says_what_is_wrong(
            self, link_bridge_auth, monkeypatch):
        """A 401 body is not JSON; .json() would raise JSONDecodeError and hide
        the cause. The error has to name the env var."""
        monkeypatch.delenv(T.TOKEN_ENV, raising=False)
        client = _remote_client(link_bridge_auth.port)
        with pytest.raises(PermissionError, match=T.TOKEN_ENV):
            client._remote_cmd({"cmd": "get_all_sessions"})

    def test_remote_cmd_with_a_stale_token_also_reports_401(
            self, link_bridge_auth, monkeypatch):
        monkeypatch.setenv(T.TOKEN_ENV, "an-old-token")
        client = _remote_client(link_bridge_auth.port)
        with pytest.raises(PermissionError):
            client._remote_cmd({"cmd": "get_all_sessions"})

    def test_no_token_anywhere_still_works(self, link_bridge_open, monkeypatch):
        monkeypatch.delenv(T.TOKEN_ENV, raising=False)
        client = _remote_client(link_bridge_open.port)
        assert client._remote_cmd({"cmd": "get_all_sessions"})["r"] == []


def _remote_client(port):
    """A driver in remote mode pointed at a test bridge, with no ports bound.

    __init__ probes and may bind, so build the remote half by hand — the same
    two attributes remote mode actually uses.
    """
    client = T.TMWebDriver.__new__(T.TMWebDriver)
    client.is_remote = True
    client.remote = f"http://127.0.0.1:{port}/link"
    client._http = requests.Session()
    client._http.trust_env = False
    return client
