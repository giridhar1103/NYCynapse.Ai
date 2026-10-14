"""The agent as an MCP host.

Live and web tools run in their own MCP server process (`nycynapse-tools`). The agent keeps one
client session to it open on a background event loop, lists its tools, and calls them from any
thread. If the server process dies it is started again; if it cannot start, the same functions
are called in process, so a broken connection never breaks an answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path

from ..tools import Tool, ToolResult, failed
from ..tools import live as live_tools
from ..tools import trips as trip_tools
from ..tools import web as web_tools

log = logging.getLogger(__name__)
CALL_TIMEOUT_S = 40


def _server_params():
    """Start the tools server with this interpreter and this copy of the code."""
    from mcp import StdioServerParameters

    src = str(Path(__file__).resolve().parents[2])
    # The trip planner reads typical car trip times from the lake, read only.
    keep = ("HOME", "PATH", "LANG", "TZ", "NYC_LAKE_PG_DSN", "NYC_LAKE_DATA_PATH")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONPATH"] = src
    return StdioServerParameters(
        command=sys.executable, args=["-m", "nycynapse.mcp_tools"], env=env
    )


class MCPHost:
    def __init__(self):
        self.local = {t.name: t for t in live_tools.TOOLS + trip_tools.TOOLS + web_tools.TOOLS}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-host")
        self._thread.start()
        self._client = None
        self._connected = threading.Event()
        self._broken: asyncio.Event | None = None
        self.remote_names: set[str] = set()
        asyncio.run_coroutine_threadsafe(self._keep_connected(), self._loop)

    async def _keep_connected(self):
        """One task owns the client session for its whole life, and opens a new one when the
        server process goes away."""
        from mcp import Client

        self._broken = asyncio.Event()
        while True:
            try:
                params = _server_params()
                async with Client(params, read_timeout_seconds=CALL_TIMEOUT_S) as client:
                    listed = await client.list_tools()
                    self.remote_names = {t.name for t in listed.tools}
                    self._client = client
                    self._connected.set()
                    log.info("connected to MCP tools server: %s", sorted(self.remote_names))
                    self._broken.clear()
                    await self._broken.wait()
            except Exception as e:  # noqa: BLE001 - keep trying in the background
                log.warning("MCP tools server connection failed: %s", e)
            self._client = None
            self._connected.clear()
            await asyncio.sleep(3)

    def _mark_broken(self) -> None:
        if self._broken is not None:
            self._loop.call_soon_threadsafe(self._broken.set)

    def call(self, name: str, args: dict) -> tuple[ToolResult, str]:
        """Call a tool; returns the result and how it was reached (mcp or local)."""
        tool = self.local.get(name)
        if tool is None:
            return failed(f"no tool called {name}"), "none"
        if self._connected.wait(timeout=8) and self._client is not None:
            client = self._client
            try:
                fut = asyncio.run_coroutine_threadsafe(client.call_tool(name, args), self._loop)
                res = fut.result(timeout=CALL_TIMEOUT_S + 5)
                text = "".join(getattr(c, "text", "") for c in res.content)
                if res.is_error:
                    return failed(text[:300] or "tool error"), "mcp"
                d = json.loads(text)
                keys = ("summary", "data", "sources", "map", "ok", "error", "cost_usd")
                return ToolResult(**{k: d[k] for k in keys if k in d}), "mcp"
            except TimeoutError:
                return failed("the tool took too long"), "mcp"
            except Exception as e:  # noqa: BLE001 - reconnect in the background, answer now
                log.warning("MCP call %s failed: %s", name, e)
                self._mark_broken()
        return tool(**args), "local"

    def specs(self) -> list[Tool]:
        return [t for t in self.local.values() if t.enabled()]
