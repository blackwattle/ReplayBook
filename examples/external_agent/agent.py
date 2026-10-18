"""A tiny example of an external agent that Replaybook can test.

It reads the ticket and the MCP server config from the environment variables the external
adapter sets, connects to the mock ticket and kafka servers over stdio, and handles one
request type (new Kafka topic) with a couple of regular expressions. Anything else, it
leaves a comment and stops. Run it through Replaybook with:

    replaybook replay --agent external --command "python examples/external_agent/agent.py" ...
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def connect(stack: AsyncExitStack, server: dict) -> ClientSession:
    params = StdioServerParameters(
        command=server["command"], args=server["args"], env=server.get("env")
    )
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def main() -> None:
    config = json.loads(open(os.environ["REPLAYBOOK_MCP_CONFIG"], encoding="utf-8").read())
    servers = config["mcpServers"]
    async with AsyncExitStack() as stack:
        platform = await connect(stack, servers["ticket"])
        ticket = (await platform.call_tool("ticket_get", {})).structured_content
        text = f"{ticket['summary']}\n{ticket['description']}"
        topic = re.search(r"\b([a-z0-9-]+\.[a-z]+\.v\d)\b", text)
        partitions = re.search(
            r"(\d+)\s+partitions?|partitions?[^\d]{0,12}(\d+)", text, re.IGNORECASE
        )
        env = re.search(r"\b(dev|test|uat|prod)\b", text, re.IGNORECASE)
        if "kafka" not in text.lower() or not topic:
            await platform.call_tool(
                "ticket_comment", {"body": "Not a Kafka topic request; skipping."}
            )
            print("skipped")
            return
        if not partitions:
            await platform.call_tool(
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
        kafka = await connect(stack, servers["kafka"])
        result = await kafka.call_tool(
            "kafka_create_topic", {"cluster": cluster, "topic": topic.group(1), "partitions": count}
        )
        print(json.dumps(result.structured_content))
        await platform.call_tool("ticket_comment", {"body": f"Created {topic.group(1)}."})


if __name__ == "__main__":
    asyncio.run(main())
