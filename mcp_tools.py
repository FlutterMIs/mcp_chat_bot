"""Real MCP transport for the analyst: a long-lived stdio session to mcp_stdio_server.py.

Same call(name, args) -> dict interface as analyst.LocalTools, so the brain doesn't care which one it gets.
The MCP client is async; it runs on its own event-loop thread and call() blocks on it.
"""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent


class McpToolError(ValueError):
    """A tool rejected the call (bad column/value, missing source). ValueError so the planner can self-correct."""


class McpStdioTools:
    transport = "mcp-stdio"

    def __init__(self, env=None, timeout=120):
        self.timeout = timeout
        self._env = {**os.environ, **(env or {})}
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="mcp-client", daemon=True).start()
        self._lock = threading.Lock()
        self._session = None
        self.generation = 0          # bumps on every (re)connect: sources must be re-registered
        self._connect()

    def _run(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout or self.timeout)

    def _connect(self):
        """Open the session inside one long-lived task: anyio requires it to be closed by the same task."""
        async def open_session():
            ready = self._loop.create_future()
            stop = asyncio.Event()

            async def hold():
                try:
                    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "mcp_stdio_server.py")], cwd=str(HERE), env=self._env)
                    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                        await session.initialize()
                        ready.set_result(session)
                        await stop.wait()
                except BaseException as e:
                    if not ready.done():
                        ready.set_exception(e)

            task = asyncio.ensure_future(hold())
            return await ready, stop, task
        if self._session is not None:
            self.close(stop_loop=False)
        self._session, self._stop, self._task = self._run(open_session(), timeout=60)
        self.generation += 1

    def list_tools(self):
        return [t.name for t in self._run(self._session.list_tools()).tools]

    def call(self, name, args):
        try:
            res = self._run(self._session.call_tool(name, args))
        except (McpToolError, TimeoutError):
            raise
        except Exception:
            # Transport died (server crashed / pipe closed): reconnect once, caller re-registers sources.
            with self._lock:
                self._connect()
            raise ConnectionError("MCP server restarted; sources need re-registering")
        text = "".join(getattr(c, "text", "") for c in res.content)
        if res.isError:
            raise McpToolError(text.replace(f"Error executing tool {name}: ", "", 1))
        if res.structuredContent is not None:
            data = res.structuredContent
            return data.get("result", data) if set(data) == {"result"} else data
        return json.loads(text) if text else {}

    def close(self, stop_loop=True):
        async def stop():
            self._stop.set()
            await asyncio.wait_for(self._task, 10)
        try:
            self._run(stop(), timeout=15)
        except Exception:
            pass
        if stop_loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
