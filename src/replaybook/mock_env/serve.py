"""Run one mock server over stdio for agents that live in another process."""

from __future__ import annotations

from pathlib import Path

from replaybook.mock_env.knowledge import Corpus
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.registry import build_server, server_names
from replaybook.mock_env.world import WorldState


def serve_stdio(
    server: str,
    world_path: Path | None = None,
    record_path: Path | None = None,
    knowledge_dir: Path | None = None,
) -> None:
    known = server_names()
    if server not in known:
        raise SystemExit(f"unknown server '{server}'. Choose from: {', '.join(known)}")
    if world_path is not None:
        world = WorldState.model_validate_json(Path(world_path).read_text(encoding="utf-8"))
    else:
        world = WorldState()
    recorder = CallRecorder(sink=record_path)
    mock = build_server(server, world, recorder, corpus=Corpus.maybe(knowledge_dir))
    mock.mcp.run("stdio")
