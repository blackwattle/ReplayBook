"""Serve the mock servers to agents that live in another process: over stdio (one server, or
all of them combined) or over streamable HTTP.

Whichever door an agent uses, the tools are the same recording wrappers over the same world
state, every call lands in the same record file and the knowledge tools sit behind the same
time-travel guard, so scores are comparable across doors. When the port is 0 the HTTP server
takes a free one and announces `REPLAYBOOK_LISTENING <url>` on stdout before it starts
serving, so a parent process can read the line instead of polling.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from replaybook.mock_env.knowledge import Corpus
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.registry import MockEnvironment, server_names
from replaybook.mock_env.world import WorldState

ALL = "all"
MCP_PATH = "/mcp"
READY_PREFIX = "REPLAYBOOK_LISTENING "


def load_world(world_path: Path | None) -> WorldState:
    if world_path is None:
        return WorldState()
    return WorldState.model_validate_json(Path(world_path).read_text(encoding="utf-8"))


def build_environment(
    servers: list[str] | None,
    world_path: Path | None = None,
    record_path: Path | None = None,
    knowledge_dir: Path | None = None,
) -> MockEnvironment:
    """The mock environment for one case, limited to `servers` (None or ["all"]: every
    server), recording to `record_path`."""
    known = server_names()
    wanted = known if not servers or servers == [ALL] else list(servers)
    unknown = [s for s in wanted if s not in known]
    if unknown:
        raise SystemExit(f"unknown server(s) {', '.join(unknown)}. Choose from: {', '.join(known)}")
    return MockEnvironment(
        load_world(world_path),
        CallRecorder(sink=record_path),
        servers=wanted,
        knowledge=Corpus.maybe(knowledge_dir),
    )


def combined_mcp(env: MockEnvironment) -> MCPServer:
    """One MCP server carrying every tool of the environment. Tool names are unique across
    servers (ticket_*, knowledge_*, <pack>_*), so nothing collides."""
    if len(env.servers) == 1:
        return next(iter(env.servers.values())).mcp
    instructions = "\n\n".join(
        f"{name}: {server.mcp.instructions}" for name, server in env.servers.items()
    )
    combined = MCPServer(name="replaybook-mock", instructions=instructions)
    for server in env.servers.values():
        for tool in server.mcp._tool_manager.list_tools():  # noqa: SLF001
            combined.add_tool(
                server.tools[tool.name],
                name=tool.name,
                description=tool.description,
                annotations=tool.annotations,
            )
    return combined


def serve_stdio(
    server: str,
    world_path: Path | None = None,
    record_path: Path | None = None,
    knowledge_dir: Path | None = None,
) -> None:
    env = build_environment([server], world_path, record_path, knowledge_dir)
    combined_mcp(env).run("stdio")


def listening_socket(host: str, port: int) -> socket.socket:
    """Bind and listen before anything is announced; port 0 means a free one."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    return sock


def announce_stdout(line: str) -> None:
    print(line, flush=True)


def serve_http(
    servers: list[str] | None,
    world_path: Path | None = None,
    record_path: Path | None = None,
    knowledge_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    announce: Callable[[str], None] = announce_stdout,
) -> None:
    """Serve the mock servers over streamable HTTP at http://host:port/mcp until killed."""
    import uvicorn

    logging.getLogger("mcp").setLevel(logging.WARNING)  # the SDK's INFO lines are not ours
    env = build_environment(servers, world_path, record_path, knowledge_dir)
    app = combined_mcp(env).streamable_http_app(
        streamable_http_path=MCP_PATH, stateless_http=True, json_response=True, host=host
    )
    sock = listening_socket(host, port)
    bound = sock.getsockname()[1]
    announce(f"{READY_PREFIX}http://{host}:{bound}{MCP_PATH}")
    config = uvicorn.Config(app, host=host, port=bound, log_level="warning", access_log=False)
    uvicorn.Server(config).run(sockets=[sock])
