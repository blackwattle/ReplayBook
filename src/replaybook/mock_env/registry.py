"""All mock servers for one case, callable in-process, plus tool-schema fingerprints.

The environment hosts the core ticket server, the knowledge server (past tickets behind
the time-travel guard) and one server per loaded pack. The fingerprint hashes exactly
what an MCP client would see (names, descriptions, input schemas, annotations), so a
changed tool surface changes the fingerprint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from typing import Any

from replaybook.mock_env import knowledge, ticket
from replaybook.mock_env.base import MockServer
from replaybook.mock_env.recorder import CallKind, CallRecorder
from replaybook.mock_env.ticket import ASK_TOOL, COMMENT_TOOL, GET_TOOL, NOTE_TOOLS
from replaybook.mock_env.world import WorldState
from replaybook.packs import PackRegistry, registry

__all__ = [
    "ASK_TOOL",
    "COMMENT_TOOL",
    "GET_TOOL",
    "NOTE_TOOLS",
    "MockEnvironment",
    "describe_all_tools",
    "server_names",
    "tool_schema_fingerprint",
]


def server_names(packs: PackRegistry | None = None) -> list[str]:
    """The ticket and knowledge servers plus one server per pack."""
    reg = packs if packs is not None else registry()
    return [ticket.SERVER_NAME, knowledge.SERVER_NAME, *(p.name for p in reg.packs)]


def build_server(
    name: str,
    world: WorldState,
    recorder: CallRecorder,
    packs: PackRegistry | None = None,
    corpus: knowledge.Corpus | None = None,
) -> MockServer:
    if name == ticket.SERVER_NAME:
        return ticket.build(world, recorder)
    if name == knowledge.SERVER_NAME:
        return knowledge.build(world, recorder, corpus)
    reg = packs if packs is not None else registry()
    return reg.pack(name).build_server(world, recorder)


class MockEnvironment:
    def __init__(
        self,
        world: WorldState | None = None,
        recorder: CallRecorder | None = None,
        servers: Iterable[str] | None = None,
        packs: PackRegistry | None = None,
        knowledge: knowledge.Corpus | None = None,
    ) -> None:
        self.packs = packs if packs is not None else registry()
        self.world = world if world is not None else WorldState()
        self.recorder = recorder if recorder is not None else CallRecorder()
        self.knowledge = knowledge
        names = list(servers) if servers is not None else server_names(self.packs)
        self.servers: dict[str, MockServer] = {
            name: build_server(name, self.world, self.recorder, self.packs, knowledge)
            for name in names
        }
        self.tools: dict[str, Any] = {}
        self.kinds: dict[str, CallKind] = {}
        self.destructive: set[str] = set()
        for server in self.servers.values():
            self.tools.update(server.tools)
            self.kinds.update(server.kinds)
            self.destructive |= server.destructive

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool by name the way an in-process agent does. Never raises for tool-level
        problems; the agent gets an error result instead."""
        fn = self.tools.get(name)
        if fn is None:
            return {"error": f"unknown tool '{name}'"}
        try:
            return fn(**(arguments or {}))
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}

    def describe_tools(self) -> list[dict[str, Any]]:
        """Tool descriptions exactly as an MCP client would list them."""
        return asyncio.run(self._describe())

    async def _describe(self) -> list[dict[str, Any]]:
        described: list[dict[str, Any]] = []
        for server_name, server in self.servers.items():
            for tool in await server.mcp.list_tools():
                described.append(
                    {
                        "server": server_name,
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                        "annotations": tool.annotations.model_dump(exclude_none=True)
                        if tool.annotations
                        else {},
                    }
                )
        return sorted(described, key=lambda d: d["name"])


def tool_schema_fingerprint(descriptions: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of the tool surface."""
    canonical = json.dumps(
        sorted(descriptions, key=lambda d: d["name"]), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def describe_all_tools(packs: PackRegistry | None = None) -> list[dict[str, Any]]:
    """Describe the full tool surface against an empty world (for fingerprints and docs)."""
    return MockEnvironment(WorldState(), packs=packs).describe_tools()
