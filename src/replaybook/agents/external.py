"""Run any agent as a subprocess pointed at the mock servers.

The adapter writes the case's world state to a temp directory, builds an MCP config that
starts each mock server over stdio (with the world and a per-server record file), and runs
the agent command with two environment variables:

- REPLAYBOOK_MCP_CONFIG: path to a JSON file in the common `{"mcpServers": {...}}` shape
- REPLAYBOOK_TICKET: path to the ticket context as JSON

Two more doors reach the same recorded tools for agents that cannot start stdio servers
(another container, a hosted agent, a framework without MCP). `transport="http"` starts one
mock server over streamable HTTP per case and sets REPLAYBOOK_MCP_URL (the MCP config then
holds `{"type": "http", "url": ...}`); `transport="tools"` starts the function-calling
endpoint instead and sets REPLAYBOOK_TOOLS_URL. Whichever door, the world, the recorder and
the time-travel guard are the same, so scores are comparable.

When the agent exits, the recorded calls from every server are merged by time and handed
to the environment's recorder so scoring works the same as for in-process agents.

Launchers (`replaybook.agents.launchers`) use the same plumbing to run an off-the-shelf
agent harness: `case_files` writes the per-case files, `run_command` runs the process and
`collect_calls` hands the recorded calls back.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from replaybook.agents.base import AgentRunResult
from replaybook.mock_env.recorder import CallRecorder, RecordedCall
from replaybook.mock_env.registry import MockEnvironment, server_names
from replaybook.mock_env.serve import READY_PREFIX

TRANSPORTS = ("stdio", "http", "tools")
STARTUP_TIMEOUT_S = 60.0


def server_command(
    name: str, world_path: Path, record_path: Path, knowledge_dir: Path | None = None
) -> dict[str, Any]:
    """How to start one mock server over stdio: `{"command": ..., "args": [...]}`."""
    args = [
        "-m",
        "replaybook.cli",
        "mock-server",
        "--server",
        name,
        "--world",
        str(world_path),
        "--record",
        str(record_path),
    ]
    if knowledge_dir is not None:
        args += ["--knowledge", str(knowledge_dir)]
    return {"command": sys.executable, "args": args}


def mcp_config(
    world_path: Path, record_dir: Path, servers: list[str], knowledge_dir: Path | None = None
) -> dict[str, Any]:
    return {
        "mcpServers": {
            name: server_command(name, world_path, record_dir / f"{name}.jsonl", knowledge_dir)
            for name in servers
        }
    }


@dataclass
class CaseFiles:
    """The per-case files an agent process works from, all inside one temp directory."""

    root: Path
    servers: list[str]
    knowledge_dir: Path | None

    @property
    def world_path(self) -> Path:
        return self.root / "replaybook" / "world.json"

    @property
    def ticket_path(self) -> Path:
        return self.root / "replaybook" / "ticket.json"

    @property
    def record_dir(self) -> Path:
        return self.root / "replaybook" / "records"

    @property
    def config_path(self) -> Path:
        return self.root / "replaybook" / "mcp.json"

    @property
    def workdir(self) -> Path:
        """An empty directory the agent process runs in."""
        return self.root / "work"

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        return mcp_config(self.world_path, self.record_dir, self.servers, self.knowledge_dir)[
            "mcpServers"
        ]

    def environment(self) -> dict[str, str]:
        """The two variables of the plain external-agent contract, on top of the parent's."""
        return {
            **os.environ,
            "REPLAYBOOK_MCP_CONFIG": str(self.config_path),
            "REPLAYBOOK_TICKET": str(self.ticket_path),
        }


@contextmanager
def case_files(env: MockEnvironment) -> Iterator[CaseFiles]:
    """Write the world, the ticket and the MCP config for one case; clean up afterwards."""
    servers = sorted(env.servers)
    knowledge_dir = env.knowledge.path if env.knowledge is not None else None
    with tempfile.TemporaryDirectory(prefix="replaybook-ext-", ignore_cleanup_errors=True) as tmp:
        files = CaseFiles(Path(tmp), servers, knowledge_dir)
        files.record_dir.mkdir(parents=True)
        files.workdir.mkdir()
        files.world_path.write_text(env.world.model_dump_json(), encoding="utf-8")
        ticket = env.world.ticket.model_dump() if env.world.ticket else {}
        files.ticket_path.write_text(json.dumps(ticket, indent=2), encoding="utf-8")
        files.config_path.write_text(
            json.dumps({"mcpServers": files.mcp_servers()}, indent=2), encoding="utf-8"
        )
        yield files


@dataclass
class Door:
    """A mock server process serving one case over HTTP, and where to reach it."""

    transport: str
    url: str
    process: subprocess.Popen[str]


def _reader(stream: Any, lines: queue.Queue[str | None]) -> None:
    for line in stream:
        lines.put(line.rstrip("\r\n"))
    lines.put(None)


@contextmanager
def open_door(
    files: CaseFiles, transport: str, startup_timeout_s: float = STARTUP_TIMEOUT_S
) -> Iterator[Door]:
    """Start the HTTP mock server (transport http) or the function-calling endpoint
    (transport tools) for one case on a free port, wait for its ready line, stop it after."""
    if transport == "http":
        args = ["mock-server", "--server", "all", "--http", "--port", "0"]
    elif transport == "tools":
        args = ["tool-endpoint", "--port", "0"]
    else:
        raise ValueError(f"no HTTP door for transport {transport!r}")
    args += ["--world", str(files.world_path), "--record", str(files.record_dir / "all.jsonl")]
    if files.knowledge_dir is not None:
        args += ["--knowledge", str(files.knowledge_dir)]
    process = subprocess.Popen(
        [sys.executable, "-m", "replaybook.cli", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(files.workdir),
    )
    lines: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, lines), daemon=True).start()
    try:
        url = _wait_ready(process, lines, startup_timeout_s)
        yield Door(transport=transport, url=url, process=process)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _wait_ready(
    process: subprocess.Popen[str], lines: queue.Queue[str | None], timeout_s: float
) -> str:
    try:
        while True:
            line = lines.get(timeout=timeout_s)
            if line is None:
                break
            if line.startswith(READY_PREFIX):
                return line[len(READY_PREFIX) :].strip()
    except queue.Empty:
        raise RuntimeError(f"the mock server did not report ready within {timeout_s}s") from None
    stderr = process.stderr.read() if process.stderr else ""
    raise RuntimeError(f"the mock server exited before it was ready: {stderr[-2000:]}")


def run_command(
    command: list[str],
    child_env: dict[str, str],
    timeout_s: float,
    cwd: Path | None = None,
    stdin: str | None = None,
) -> tuple[str, str | None]:
    """Run the agent process. Returns (stdout, error); a non-zero exit or a timeout is an
    error, with the tail of stderr."""
    try:
        completed = subprocess.run(
            command,
            env=child_env,
            cwd=str(cwd) if cwd else None,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout_s}s"
    except OSError as exc:  # the command does not exist or cannot be started
        return "", f"could not start {command[0]!r}: {exc}"
    error = None
    if completed.returncode != 0:
        error = f"exit code {completed.returncode}: {completed.stderr[-2000:]}"
    return completed.stdout, error


def collect_calls(env: MockEnvironment, files: CaseFiles) -> list[RecordedCall]:
    """Merge the record files by time and hand the calls to the environment's recorder."""
    calls: list[RecordedCall] = []
    for path in sorted(files.record_dir.glob("*.jsonl")):
        calls.extend(CallRecorder.load(path))
    calls.sort(key=lambda c: (c.at, c.seq))
    for call in calls:
        env.recorder.record(call.tool, call.kind, call.args, call.result, call.error)
    return calls


def transcript_of(calls: list[RecordedCall]) -> list[dict[str, Any]]:
    return [{"tool": c.tool, "args": c.args, "result": c.result, "error": c.error} for c in calls]


class ExternalAgent:
    name = "external"

    def __init__(
        self,
        command: list[str],
        timeout_s: float = 180.0,
        label: str | None = None,
        transport: str = "stdio",
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        if transport not in TRANSPORTS:
            raise ValueError(f"transport must be one of {', '.join(TRANSPORTS)}, not {transport!r}")
        self.command = list(command)
        self.timeout_s = timeout_s
        self.label = label or Path(command[-1]).name
        self.transport = transport

    @property
    def config(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "version": 1,
            "command": self.command,
            "label": self.label,
            "transport": self.transport,
        }

    def run(self, env: MockEnvironment) -> AgentRunResult:
        with case_files(env) as files:
            child_env = files.environment()
            if self.transport == "stdio":
                stdout, error = run_command(self.command, child_env, self.timeout_s)
            else:
                try:
                    with open_door(files, self.transport) as door:
                        if door.transport == "http":
                            child_env["REPLAYBOOK_MCP_URL"] = door.url
                            config = {"replaybook": {"type": "http", "url": door.url}}
                            files.config_path.write_text(
                                json.dumps({"mcpServers": config}, indent=2), encoding="utf-8"
                            )
                        else:
                            child_env["REPLAYBOOK_TOOLS_URL"] = door.url
                        stdout, error = run_command(self.command, child_env, self.timeout_s)
                except RuntimeError as exc:
                    stdout, error = "", str(exc)
            calls = collect_calls(env, files)
        return AgentRunResult(
            transcript=transcript_of(calls),
            final_message=stdout.strip()[-2000:],
            stopped_reason="error" if error else "done",
            error=error,
        )


def default_servers() -> list[str]:
    return sorted(server_names())
