"""Shared plumbing for the mock MCP servers.

Every tool is registered twice from one function: on an MCPServer (so external agents can
use it over stdio) and in a plain dict (so in-process agents can call it directly). The
wrapper records each call with its kind, and turns ToolError into an error result the
agent can read instead of a crash.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from replaybook.mock_env.recorder import CallKind, CallRecorder
from replaybook.mock_env.world import WorldState

ToolFn = Callable[..., dict[str, Any]]


class ToolError(Exception):
    """A failure the agent should see as a tool result (not found, already exists, retired)."""


class MockServer:
    def __init__(self, name: str, instructions: str, world: WorldState, recorder: CallRecorder):
        self.name = name
        self.world = world
        self.recorder = recorder
        self.mcp = MCPServer(name=f"replaybook-mock-{name}", instructions=instructions)
        self.tools: dict[str, ToolFn] = {}
        self.kinds: dict[str, CallKind] = {}
        self.destructive: set[str] = set()

    def read(self, fn: ToolFn) -> ToolFn:
        """Register a read-only tool."""
        return self._register(fn, "read", destructive=False, idempotent=True)

    def write(
        self, *, destructive: bool = False, idempotent: bool = False
    ) -> Callable[[ToolFn], ToolFn]:
        """Register a write tool. Writes are recorded and applied to the in-memory world only."""

        def decorate(fn: ToolFn) -> ToolFn:
            return self._register(fn, "write", destructive=destructive, idempotent=idempotent)

        return decorate

    def _register(self, fn: ToolFn, kind: CallKind, destructive: bool, idempotent: bool) -> ToolFn:
        name = fn.__name__
        signature = inspect.signature(fn)
        recorder = self.recorder

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            call_args = dict(bound.arguments)
            try:
                result = fn(*args, **kwargs)
            except ToolError as exc:
                recorder.record(name, kind, call_args, result=None, error=str(exc))
                return {"error": str(exc)}
            recorder.record(name, kind, call_args, result=result)
            return result

        annotations = ToolAnnotations(
            title=name.replace("_", " "),
            read_only_hint=kind == "read",
            destructive_hint=destructive,
            idempotent_hint=idempotent,
            open_world_hint=False,
        )
        self.mcp.tool(name=name, description=inspect.getdoc(fn), annotations=annotations)(wrapper)
        self.tools[name] = wrapper
        self.kinds[name] = kind
        if destructive:
            self.destructive.add(name)
        return wrapper
