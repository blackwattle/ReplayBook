"""Run any agent as a subprocess pointed at the mock servers.

The adapter writes the case's world state to a temp directory, builds an MCP config that
starts each mock server over stdio (with the world and a per-server record file), and runs
the agent command with two environment variables:

- REPLAYBOOK_MCP_CONFIG: path to a JSON file in the common `{"mcpServers": {...}}` shape
- REPLAYBOOK_TICKET: path to the ticket context as JSON

When the agent exits, the recorded calls from every server are merged by time and handed
to the environment's recorder so scoring works the same as for in-process agents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from replaybook.agents.base import AgentRunResult
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.registry import MockEnvironment, server_names


def mcp_config(
    world_path: Path, record_dir: Path, servers: list[str], knowledge_dir: Path | None = None
) -> dict[str, Any]:
    config: dict[str, Any] = {"mcpServers": {}}
    for name in servers:
        args = [
            "-m",
            "replaybook.cli",
            "mock-server",
            "--server",
            name,
            "--world",
            str(world_path),
            "--record",
            str(record_dir / f"{name}.jsonl"),
        ]
        if knowledge_dir is not None:
            args += ["--knowledge", str(knowledge_dir)]
        config["mcpServers"][name] = {"command": sys.executable, "args": args}
    return config


class ExternalAgent:
    name = "external"

    def __init__(
        self, command: list[str], timeout_s: float = 180.0, label: str | None = None
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = list(command)
        self.timeout_s = timeout_s
        self.label = label or Path(command[-1]).name

    @property
    def config(self) -> dict[str, Any]:
        return {"agent": self.name, "version": 1, "command": self.command, "label": self.label}

    def run(self, env: MockEnvironment) -> AgentRunResult:
        servers = sorted(env.servers)
        with tempfile.TemporaryDirectory(prefix="replaybook-ext-") as tmp:
            tmp_path = Path(tmp)
            world_path = tmp_path / "world.json"
            world_path.write_text(env.world.model_dump_json(), encoding="utf-8")
            ticket_path = tmp_path / "ticket.json"
            ticket = env.world.ticket.model_dump() if env.world.ticket else {}
            ticket_path.write_text(json.dumps(ticket, indent=2), encoding="utf-8")
            record_dir = tmp_path / "records"
            record_dir.mkdir()
            config_path = tmp_path / "mcp.json"
            knowledge_dir = env.knowledge.path if env.knowledge is not None else None
            config_path.write_text(
                json.dumps(mcp_config(world_path, record_dir, servers, knowledge_dir), indent=2),
                encoding="utf-8",
            )
            child_env = {
                **os.environ,
                "REPLAYBOOK_MCP_CONFIG": str(config_path),
                "REPLAYBOOK_TICKET": str(ticket_path),
            }
            error: str | None = None
            stdout = ""
            try:
                completed = subprocess.run(
                    self.command,
                    env=child_env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
                stdout = completed.stdout
                if completed.returncode != 0:
                    error = f"exit code {completed.returncode}: {completed.stderr[-2000:]}"
            except subprocess.TimeoutExpired:
                error = f"timed out after {self.timeout_s}s"

            calls = []
            for name in servers:
                calls.extend(CallRecorder.load(record_dir / f"{name}.jsonl"))
            calls.sort(key=lambda c: (c.at, c.seq))
            for call in calls:
                env.recorder.record(call.tool, call.kind, call.args, call.result, call.error)

        transcript = [
            {"tool": c.tool, "args": c.args, "result": c.result, "error": c.error} for c in calls
        ]
        return AgentRunResult(
            transcript=transcript,
            final_message=stdout.strip()[-2000:],
            stopped_reason="error" if error else "done",
            error=error,
        )


def default_servers() -> list[str]:
    return sorted(server_names())
