"""A fake agent CLI that stands in for a real harness in the launcher tests.

It is started the way the launcher under test would start the real thing, with the same
arguments, environment, working directory and stdin. It records all of that to the file
named by FAKE_HARNESS_DUMP, finds the MCP servers the way the real harness would
(FAKE_HARNESS_CONFIG says where to look: "arg:<flag>", "file:<relative path>",
"env:<variable>" or "flags"), connects to the ticket server over stdio like any MCP
client, reads the ticket, and makes one write on the kafka server if there is one. A
launcher only passes the test if a process reading its output the harness's way can reach
the recorded mock tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def servers_from_mcp_servers(document: dict[str, Any], key: str) -> dict[str, list[str]]:
    """`{"<key>": {name: {"command": ..., "args": [...]} | {"command": [...]}}}` -> argv."""
    found: dict[str, list[str]] = {}
    for name, entry in (document.get(key) or {}).items():
        command = entry["command"]
        if isinstance(command, list):
            found[name] = list(command)
        else:
            found[name] = [command, *entry.get("args", [])]
    return found


def servers_from_flags(argv: list[str]) -> dict[str, list[str]]:
    """`-c mcp_servers.<name>.command=<json> -c mcp_servers.<name>.args=<json>` -> argv."""
    commands: dict[str, str] = {}
    args: dict[str, list[str]] = {}
    for flag, value in zip(argv, argv[1:], strict=False):
        if flag != "-c" or not value.startswith("mcp_servers."):
            continue
        key, _, raw = value.partition("=")
        _, name, field = key.split(".", 2)
        if field == "command":
            commands[name] = json.loads(raw)
        elif field == "args":
            args[name] = json.loads(raw)
    return {name: [commands[name], *args.get(name, [])] for name in commands}


def find_servers(argv: list[str], env: dict[str, str]) -> tuple[dict[str, list[str]], str | None]:
    how = env.get("FAKE_HARNESS_CONFIG", "flags")
    key = env.get("FAKE_HARNESS_SERVERS_KEY", "mcpServers")
    if how == "flags":
        return servers_from_flags(argv), None
    kind, _, where = how.partition(":")
    if kind == "arg":
        path = Path(argv[argv.index(where) + 1])
    elif kind == "file":
        path = Path.cwd() / where
    elif kind == "env":
        path = Path(env[where])
    else:
        raise SystemExit(f"unknown FAKE_HARNESS_CONFIG {how!r}")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".toml":
        import tomllib

        document = tomllib.loads(text)
    else:
        document = json.loads(text)
    return servers_from_mcp_servers(document, key), text


async def connect(stack: AsyncExitStack, argv: list[str]) -> ClientSession:
    params = StdioServerParameters(command=argv[0], args=argv[1:])
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def act(servers: dict[str, list[str]]) -> dict[str, Any]:
    async with AsyncExitStack() as stack:
        ticket_server = await connect(stack, servers["ticket"])
        ticket = (await ticket_server.call_tool("ticket_get", {})).structured_content
        done: dict[str, Any] = {"ticket_id": (ticket or {}).get("id")}
        if "kafka" in servers:
            kafka = await connect(stack, servers["kafka"])
            result = await kafka.call_tool(
                "kafka_create_topic",
                {"cluster": "kafka-dev", "topic": "fake.events.v1", "partitions": 3},
            )
            done["kafka"] = result.structured_content
        await ticket_server.call_tool("ticket_comment", {"body": "fake harness finished"})
        return done


def main() -> None:
    argv = sys.argv[1:]
    env = dict(os.environ)
    stdin = sys.stdin.read() if env.get("FAKE_HARNESS_READ_STDIN") else ""
    servers, config_text = find_servers(argv, env)
    prompt_file_text = None
    if env.get("FAKE_HARNESS_PROMPT_FILE_FLAG"):
        flag = env["FAKE_HARNESS_PROMPT_FILE_FLAG"]
        prompt_file_text = Path(argv[argv.index(flag) + 1]).read_text(encoding="utf-8")
    outcome = asyncio.run(act(servers)) if servers else {"error": "no servers found"}
    dump = {
        "argv": argv,
        "cwd": str(Path.cwd()),
        "stdin": stdin,
        "env": {
            k: v for k, v in env.items() if k.startswith(("REPLAYBOOK_", "FAKE_", "OPENCODE_"))
        },
        "servers": servers,
        "config_text": config_text,
        "prompt_file_text": prompt_file_text,
        "outcome": outcome,
    }
    if env.get("FAKE_HARNESS_DUMP"):
        Path(env["FAKE_HARNESS_DUMP"]).write_text(json.dumps(dump, indent=2), encoding="utf-8")
    if not servers:
        sys.exit("fake harness: no MCP servers found")
    print("fake harness done")
    if env.get("FAKE_HARNESS_EXIT"):
        sys.exit(int(env["FAKE_HARNESS_EXIT"]))


if __name__ == "__main__":
    main()
