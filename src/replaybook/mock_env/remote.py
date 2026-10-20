"""A connection to a team's own MCP server, for knowledge packs (`mock_env.guarded`).

Mock tools are plain functions called from threads (in-process agents, worker pools) or
from inside a running MCP server. The SDK's client is async and its connection must be
opened and closed by the same task, so one background thread runs an event loop with a
single long-lived task that owns the connection; calls are handed to that loop and waited
for. The connection opens on the first call and is reopened after a failure.

This is one of the few modules that may use the network: it connects to the one server a
pack names, over streamable HTTP or stdio. Air-gapped mode governs it like the others (the
host must be in REPLAYBOOK_ALLOWED_HOSTS), and only read-only tools ever reach it.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
from contextlib import AsyncExitStack
from typing import Any

from replaybook.mock_env.guarded import ServerSpec, expand_env


class RemoteError(RuntimeError):
    """The server could not be reached or did not answer."""


def result_to_dict(result: Any) -> dict[str, Any]:
    """A CallToolResult as a plain object: the structured content when there is one,
    otherwise the text parsed as JSON, otherwise the text; errors as {"error": ...}."""
    text = "\n".join(
        str(getattr(block, "text", "")) for block in (getattr(result, "content", None) or [])
    ).strip()
    if getattr(result, "is_error", False):
        return {"error": text or "the tool reported an error"}
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    if structured is not None:
        return {"result": structured}
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def root_cause(exc: BaseException) -> BaseException:
    """The first real error inside the task groups the SDK wraps failures in."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


class RemoteServer:
    """One MCP server, reachable from any thread. `target` replaces the spec's transport
    (tests pass an in-process server; nothing else should)."""

    def __init__(self, spec: ServerSpec, target: Any = None) -> None:
        self.spec = spec
        self.target = target
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._stop: asyncio.Event | None = None
        self._owner: asyncio.Future[None] | None = None
        self._connecting: asyncio.Lock | None = None

    # --- connecting ---------------------------------------------------------------------

    def _transport(self, stack: AsyncExitStack) -> Any:
        if self.target is not None:
            return self.target
        if self.spec.url:
            http = self._http_client()
            stack.push_async_callback(http.aclose)
            from mcp.client.streamable_http import streamable_http_client

            return streamable_http_client(expand_env(self.spec.url), http_client=http)
        from mcp.client.stdio import StdioServerParameters

        env = {**os.environ, **{k: expand_env(v) for k, v in self.spec.env.items()}}
        command = [expand_env(part) for part in self.spec.command]
        return StdioServerParameters(
            command=command[0],
            args=command[1:],
            env=env,
            cwd=str(self.spec.cwd) if self.spec.cwd else None,
        )

    def _http_client(self) -> Any:
        """The SDK's own client settings, plus the pack's headers and, for a server behind a
        private certificate authority, its CA bundle. Proxy and certificate environment
        variables (HTTPS_PROXY, SSL_CERT_FILE) are honoured as usual."""
        import httpx2
        from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

        kwargs: dict[str, Any] = {
            "timeout": httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
            "headers": {k: expand_env(v) for k, v in self.spec.headers.items()},
        }
        ca_path = self.spec.ca_path()
        if ca_path is not None:
            kwargs["verify"] = ssl.create_default_context(cafile=str(ca_path))
        return httpx2.AsyncClient(**kwargs)

    async def _own(self, ready: asyncio.Future[None]) -> None:
        """The task that holds the connection: open it, report ready, wait for stop."""
        from mcp.client.client import Client

        try:
            async with AsyncExitStack() as stack:
                client = Client(
                    self._transport(stack), read_timeout_seconds=self.spec.timeout_s, cache=None
                )
                self._client = await stack.enter_async_context(client)
                self._stop = asyncio.Event()
                ready.set_result(None)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to whoever is waiting
            if not ready.done():
                ready.set_exception(exc)
        finally:
            self._client = None

    def _start_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="replaybook-remote", daemon=True).start()
            self._loop = loop
        return self._loop

    async def _connect(self) -> None:
        if self._connecting is None:
            self._connecting = asyncio.Lock()  # created on the loop that uses it
        async with self._connecting:
            if self._client is not None:
                return
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._owner = asyncio.ensure_future(self._own(ready))
            await ready

    async def _disconnect(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._owner is not None:
            try:
                await asyncio.wait_for(self._owner, timeout=10)
            except Exception:  # noqa: BLE001 - closing a broken connection
                pass
        self._owner = None
        self._stop = None
        self._client = None

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        await self._connect()
        try:
            return await self._client.call_tool(name, args)
        except Exception:
            await self._disconnect()
            raise

    async def _list(self) -> Any:
        await self._connect()
        return await self._client.list_tools()

    def _run(self, coroutine: Any) -> Any:
        with self._lock:
            loop = self._start_loop()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=self.spec.timeout_s + 15)
        except TimeoutError as exc:
            future.cancel()
            raise RemoteError(f"no answer within {self.spec.timeout_s}s") from exc
        except RemoteError:
            raise
        except Exception as exc:
            cause = root_cause(exc)
            raise RemoteError(f"{type(cause).__name__}: {cause}") from exc

    # --- the two calls a knowledge pack needs -------------------------------------------

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return result_to_dict(self._run(self._call(name, args)))

    def list_tools(self) -> list[dict[str, Any]]:
        listed = self._run(self._list())
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.input_schema,
                "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else {},
            }
            for t in listed.tools
        ]

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            self._run(self._disconnect())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None
