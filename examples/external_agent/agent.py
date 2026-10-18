"""A tiny example of an external agent that Replaybook can test.

It reads the ticket and the MCP servers from the environment variables the external
adapter sets, handles one request type (new Kafka topic) with a couple of regular
expressions, and leaves a comment for anything else. It works through either MCP door:

- stdio (the default): REPLAYBOOK_MCP_CONFIG names a JSON file in the common `mcpServers`
  shape, one server process per pack; the agent connects to the ones it needs.
- streamable HTTP (`--transport http`): REPLAYBOOK_MCP_URL is one MCP server with every
  tool, reached with the SDK's high-level client.

Run it through Replaybook with:

    replaybook replay --agent external --command "python examples/external_agent/agent.py" ...
    replaybook replay --agent external --transport http --command "python .../agent.py" ...
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client


class Tools:
    """call(tool, arguments) over whichever door Replaybook opened."""

    def __init__(self, stack: AsyncExitStack) -> None:
        self.stack = stack
        self.url = os.environ.get("REPLAYBOOK_MCP_URL")
        self.config = json.loads(open(os.environ["REPLAYBOOK_MCP_CONFIG"], encoding="utf-8").read())
        self.sessions: dict[str, Any] = {}

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        if self.url:
            if "http" not in self.sessions:
                self.sessions["http"] = await self.stack.enter_async_context(Client(self.url))
            result = await self.sessions["http"].call_tool(tool, arguments)
        else:
            if server not in self.sessions:
                spec = self.config["mcpServers"][server]
                params = StdioServerParameters(
                    command=spec["command"], args=spec["args"], env=spec.get("env")
                )
                read, write = await self.stack.enter_async_context(stdio_client(params))
                session = await self.stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[server] = session
            result = await self.sessions[server].call_tool(tool, arguments)
        return result.structured_content


async def main() -> None:
    async with AsyncExitStack() as stack:
        tools = Tools(stack)
        ticket = await tools.call("ticket", "ticket_get", {})
        text = f"{ticket['summary']}\n{ticket['description']}"
        topic = re.search(r"\b([a-z0-9-]+\.[a-z]+\.v\d)\b", text)
        partitions = re.search(
            r"(\d+)\s+partitions?|partitions?[^\d]{0,12}(\d+)", text, re.IGNORECASE
        )
        env = re.search(r"\b(dev|test|uat|prod)\b", text, re.IGNORECASE)
        if "kafka" not in text.lower() or not topic:
            await tools.call(
                "ticket", "ticket_comment", {"body": "Not a Kafka topic request; skipping."}
            )
            print("skipped")
            return
        if not partitions:
            await tools.call(
                "ticket",
                "ticket_ask_requester",
                {"question": "How many partitions does the topic need?"},
            )
            print("asked")
            return
        count = int(partitions.group(1) or partitions.group(2))
        cluster = {
            "dev": "kafka-dev",
            "test": "kafka-dev",
            "uat": "kafka-uat",
            "prod": "kafka-prod",
        }[(env.group(1).lower() if env else "dev")]
        result = await tools.call(
            "kafka",
            "kafka_create_topic",
            {"cluster": cluster, "topic": topic.group(1), "partitions": count},
        )
        print(json.dumps(result))
        await tools.call("ticket", "ticket_comment", {"body": f"Created {topic.group(1)}."})


if __name__ == "__main__":
    asyncio.run(main())
