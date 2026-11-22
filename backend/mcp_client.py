"""MCP client: starts the local MCP server as a child process over stdio and wraps every tool
call with retries and an audit record (LLD §6 "Audit log", §13).

Each call records the tool name, arguments, row count, bytes and duration, and emits a
``tool_call`` event. That powers the UI's "Data stays local" panel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

from .config import ROOT, Settings
from .graph.state import emit

log = logging.getLogger(__name__)

SERVER_SCRIPT = ROOT / "mcp_server" / "server.py"
ATTEMPTS = 3                 # first try + 2 retries
CALL_TIMEOUT = timedelta(seconds=60)
STARTUP_TIMEOUT_S = 30


class McpToolError(RuntimeError):
    pass


def _result_text(result: Any) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")


def parse_tool_result(result: Any) -> Any:
    """Turn a CallToolResult into plain Python data.

    FastMCP returns list/str results twice: as ``structuredContent = {"result": ...}`` and as
    text blocks. Prefer the structured form; fall back to JSON in the text blocks.
    """
    sc = result.structuredContent
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    if not texts:
        return []
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    return [json.loads(t) for t in texts]


class McpClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: ClientSession | None = None
        self.pid: int | None = None
        self.server_name: str | None = None
        self.error: str | None = None
        self.audit: deque[dict] = deque(maxlen=1000)

    @property
    def up(self) -> bool:
        return self.session is not None and self.error is None

    async def start(self, stack: AsyncExitStack) -> None:
        """Launch ``python mcp_server/server.py`` as a child process and open one shared session."""
        env = {
            **get_default_environment(),
            "ERP_DB_PATH": str(self.settings.erp_db_path),
            "CONTRACTS_DIR": str(self.settings.contracts_dir),
            "UPLOADS_DIR": str(self.settings.uploads_dir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)], env=env, cwd=str(ROOT))
        errlog = sys.__stderr__ or open(os.devnull, "w")   # the child's log lines go to our console
        read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await asyncio.wait_for(session.initialize(), timeout=STARTUP_TIMEOUT_S)
        self.session = session
        self.server_name = init.serverInfo.name
        summary = await self.call_tool("get_summary")
        self.pid = summary.get("pid")
        self.error = None
        log.info("MCP server '%s' running over stdio (pid %s)", self.server_name, self.pid)

    async def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """Call a tool; retried twice, then a clear error (LLD §13)."""
        if self.session is None:
            raise McpToolError(f"MCP server is not running ({self.error or 'not started'})")
        args = arguments or {}
        last_error: Exception | None = None
        for attempt in range(1, ATTEMPTS + 1):
            t0 = time.perf_counter()
            try:
                result = await self.session.call_tool(name, args, read_timeout_seconds=CALL_TIMEOUT)
                if result.isError:
                    raise McpToolError(_result_text(result) or "the tool reported an error")
                data = parse_tool_result(result)
            except Exception as e:     # noqa: BLE001 - every failure is retried, then reported
                last_error = e
                self._record(name, args, None, t0, attempt, error=f"{type(e).__name__}: {e}")
                if attempt < ATTEMPTS:
                    await asyncio.sleep(0.25 * attempt)
                continue
            self._record(name, args, data, t0, attempt)
            return data
        raise McpToolError(f"MCP tool '{name}' failed after {ATTEMPTS} attempts: {last_error}")

    def _record(self, name: str, args: dict, data: Any, t0: float, attempt: int, error: str | None = None) -> None:
        if isinstance(data, str):
            rows, size = 1, len(data.encode("utf-8"))
        elif data is None:
            rows, size = 0, 0
        else:
            rows = len(data) if isinstance(data, list) else 1
            size = len(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        event = {
            "type": "tool_call", "tool": name, "args": args, "rows": rows, "bytes": size,
            "ms": round((time.perf_counter() - t0) * 1000), "ok": error is None, "attempt": attempt,
            "transport": "stdio", "pid": self.pid,
        }
        if isinstance(data, list) and data and isinstance(data[0], dict) and "lines" in data[0]:
            event["lines"] = sum(len(d.get("lines") or []) for d in data)
        if error:
            event["error"] = error
            log.warning("MCP %s%s attempt %d failed: %s", name, args or "", attempt, error)
        self.audit.append(event)
        emit(event)
