# Back-compat shim; the real daemon lives in agent_browser_mcp.bridge.
# Prefer: python -m agent_browser_mcp.bridge  (or: agent-browser-mcp bridge)
from agent_browser_mcp.bridge import main

raise SystemExit(main())
