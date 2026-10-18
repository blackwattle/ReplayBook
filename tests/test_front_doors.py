"""The front doors: the mock servers over streamable HTTP, the function-calling endpoint and
`tools schema`. Every door opens onto the same world, the same recorder and the same guard,
so the example agent scores the same whichever one it uses; and the air-gap allowlist still
refuses everything else."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client
from typer.testing import CliRunner

from replaybook import netguard
from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.external import ExternalAgent, case_files, open_door
from replaybook.classify import classify
from replaybook.cli import app
from replaybook.mock_env.http_tools import function_schemas
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.registry import GET_TOOL, NOTE_TOOLS, MockEnvironment, describe_all_tools
from replaybook.mock_env.world import WorldState
from replaybook.schema.ticket import Ticket
from replaybook.tools_import import list_tools_from_url

ROOT = Path(__file__).resolve().parents[1]
MCP_AGENT = ROOT / "examples" / "external_agent" / "agent.py"
TOOLS_AGENT = ROOT / "examples" / "external_agent" / "tools_agent.py"
runner = CliRunner()


def _kafka_ticket(data_dir: Path) -> Ticket:
    return next(
        t
        for t in load_tickets(data_dir)
        if classify(t)[0] == "kafka_topic"
        and t.actions
        and any(a.tool.endswith("create_topic") for a in t.actions)
        and not any(c.to_status == "Waiting on Requester" for c in t.status_changes)
    )


def _env_for(ticket: Ticket, data_dir: Path) -> MockEnvironment:
    return MockEnvironment(WorldState.from_ticket(ticket, load_platform(data_dir)))


def _writes(env: MockEnvironment) -> list[tuple[str, str]]:
    return [
        (w.tool, json.dumps(w.args, sort_keys=True))
        for w in env.recorder.writes
        if w.tool not in NOTE_TOOLS
    ]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1].split("/")[0])


def test_schema_shapes_cover_every_tool(tmp_path: Path) -> None:
    described = describe_all_tools()
    openai = function_schemas(described, "openai")
    assert len(openai) == len(described) >= 50
    create = next(t for t in openai if t["function"]["name"] == "kafka_create_topic")
    assert create["type"] == "function"
    assert "cluster" in create["function"]["parameters"]["properties"]
    mcp_shape = function_schemas(described, "mcp")
    delete = next(t for t in mcp_shape if t["name"] == "kafka_delete_topic")
    assert delete["annotations"]["destructive_hint"] is True and "inputSchema" in delete
    with pytest.raises(ValueError):
        function_schemas(described, "soap")

    result = runner.invoke(app, ["tools", "schema"])
    assert result.exit_code == 0, result.output
    assert [t["function"]["name"] for t in json.loads(result.output)] == [
        t["function"]["name"] for t in openai
    ]
    out = tmp_path / "schema.json"
    written = runner.invoke(app, ["tools", "schema", "--shape", "mcp", "--out", str(out)])
    assert written.exit_code == 0 and json.loads(out.read_text(encoding="utf-8")) == mcp_shape
    assert runner.invoke(app, ["tools", "schema", "--shape", "soap"]).exit_code == 2


def test_http_mock_server_records_like_stdio(small_dataset: Path) -> None:
    ticket = _kafka_ticket(small_dataset)
    env = _env_for(ticket, small_dataset)
    with case_files(env) as files, open_door(files, "http") as door:
        assert door.url.startswith("http://127.0.0.1:") and door.url.endswith("/mcp")

        async def act() -> dict[str, Any]:
            async with Client(door.url) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                got = await client.call_tool(GET_TOOL, {})
                created = await client.call_tool(
                    "kafka_create_topic",
                    {"cluster": "kafka-dev", "topic": "door.events.v1", "partitions": 2},
                )
                return {
                    "names": names,
                    "ticket": got.structured_content,
                    "created": created.structured_content,
                }

        outcome = _run(act())
        assert outcome["names"] == set(env.tools), "one HTTP server carries every tool"
        assert outcome["ticket"]["id"] == ticket.id
        assert outcome["created"]["topic"] == "door.events.v1"
        # tools import can read the same server over HTTP
        listed = list_tools_from_url(door.url)
        assert {t["name"] for t in listed} == set(env.tools)
        assert next(t for t in listed if t["name"] == "kafka_delete_topic")["annotations"][
            "destructive_hint"
        ]
        recorded = CallRecorder.load(files.record_dir / "all.jsonl")
    assert [c.tool for c in recorded] == [GET_TOOL, "kafka_create_topic"]
    assert recorded[1].kind == "write" and recorded[1].args["partitions"] == 2
    assert door.process.poll() is not None, "the server is stopped when the door closes"


def test_tool_endpoint_serves_schema_and_records_calls(small_dataset: Path) -> None:
    ticket = _kafka_ticket(small_dataset)
    env = _env_for(ticket, small_dataset)
    with case_files(env) as files, open_door(files, "tools") as door:
        with urllib.request.urlopen(f"{door.url}/health", timeout=10) as response:
            assert json.loads(response.read())["status"] == "ok"
        with urllib.request.urlopen(f"{door.url}/tools", timeout=10) as response:
            tools = json.loads(response.read())["tools"]
        assert {t["function"]["name"] for t in tools} == set(env.tools)
        with urllib.request.urlopen(f"{door.url}/tools?shape=mcp", timeout=10) as response:
            assert "inputSchema" in json.loads(response.read())["tools"][0]

        def post(name: str, body: Any) -> tuple[int, Any]:
            request = urllib.request.Request(
                f"{door.url}/tools/{name}",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        status, got = post(GET_TOOL, {})
        assert status == 200 and got["result"]["id"] == ticket.id
        status, created = post(
            "kafka_create_topic",
            {"arguments": {"cluster": "kafka-dev", "topic": "door.events.v1", "partitions": 2}},
        )
        assert status == 200 and created["result"]["topic"] == "door.events.v1"
        status, plain = post("ticket_comment", {"body": "arguments may be the body itself"})
        assert status == 200 and plain["result"]["commented"]
        status, missing = post("kafka_create_topic", {"cluster": "kafka-dev"})
        assert status == 400 and "error" in missing["result"]
        status, unknown = post("no_such_tool", {})
        assert status == 404
        recorded = CallRecorder.load(files.record_dir / "all.jsonl")
    assert [c.tool for c in recorded] == [GET_TOOL, "kafka_create_topic", "ticket_comment"], (
        "a call with missing arguments is rejected before the tool runs, as over MCP, so it "
        "is not recorded"
    )


@pytest.mark.parametrize(
    ("transport", "agent_path"),
    [("stdio", MCP_AGENT), ("http", MCP_AGENT), ("tools", TOOLS_AGENT)],
)
def test_example_agent_scores_the_same_through_every_door(
    small_dataset: Path, transport: str, agent_path: Path
) -> None:
    ticket = _kafka_ticket(small_dataset)
    env = _env_for(ticket, small_dataset)
    agent = ExternalAgent([sys.executable, str(agent_path)], timeout_s=180, transport=transport)
    result = agent.run(env)
    assert result.error is None, (result.error, result.final_message)
    assert agent.config["transport"] == transport
    writes = _writes(env)
    assert [w[0] for w in writes] == ["kafka_create_topic"], (writes, result.final_message)
    assert env.recorder.reads and env.recorder.reads[0].tool == GET_TOOL
    reference = _env_for(ticket, small_dataset)
    ExternalAgent([sys.executable, str(MCP_AGENT)], timeout_s=180).run(reference)
    assert writes == _writes(reference), "the door does not change what is recorded"


def test_unknown_transport_and_cli_option() -> None:
    with pytest.raises(ValueError, match="transport"):
        ExternalAgent(["x"], transport="carrier-pigeon")
    result = runner.invoke(app, ["replay", "--help"])
    assert result.exit_code == 0 and "--transport" in result.output


def test_doors_sit_behind_the_airgap_allowlist(
    small_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server process runs air-gapped (it may listen, it cannot connect out); the client
    side reaches it only when the host is allowlisted, and nothing else."""
    ticket = _kafka_ticket(small_dataset)
    env = _env_for(ticket, small_dataset)
    monkeypatch.setenv(netguard.ENV_FLAG, "true")
    monkeypatch.setenv(netguard.ENV_ALLOW, "")
    try:
        with case_files(env) as files, open_door(files, "http") as door:
            port = _port_of(door.url)
            netguard.install()  # this process: nothing allowed
            before = netguard.status()["refused_connections"]
            with pytest.raises(BaseException):  # noqa: B017 - wrapped by the client's task group
                _run(_list_names(door.url))
            assert netguard.status()["refused_connections"] > before
            assert door.process.poll() is None, "the server kept running"

            netguard.install(f"127.0.0.1:{port}")
            assert _run(_list_names(door.url)) == set(env.tools)
            other = "127.0.0.2" if os.name == "nt" else "127.0.0.1"
            with pytest.raises(netguard.AirgapViolation):
                socket.create_connection((other, port + 1 if port < 65535 else port - 1), timeout=1)
    finally:
        netguard.uninstall()


async def _list_names(url: str) -> set[str]:
    async with Client(url) as client:
        return {t.name for t in (await client.list_tools()).tools}
