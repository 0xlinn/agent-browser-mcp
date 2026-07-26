"""Standalone TMWebDriver bridge daemon.

Runs the WS (18765) + HTTP (18766) bridge as a long-lived process so its
lifetime is decoupled from any MCP server instance. MCP instances detect the
open port and connect in remote mode; if none is running, the MCP server
spawns this module detached (see server.spawn_bridge_daemon).

Run manually:  agent-browser-mcp bridge
          or:  python -m agent_browser_mcp.bridge
"""
from __future__ import annotations

import os
import time

from .tmwebdriver import TMWebDriver


def main() -> int:
    host = os.environ.get("AGENT_BROWSER_TMWD_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_BROWSER_TMWD_PORT", "18765"))
    driver = TMWebDriver(host=host, port=port)
    if driver.is_remote:
        print(f"bridge already running at {host}:{port}, exiting", flush=True)
        return 0
    print(f"bridge started: ws={port} http={port + 1} pid={os.getpid()}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
