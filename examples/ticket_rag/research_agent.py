"""A tiny research agent: the offline stand-in for an LLM agent in `knowledge eval --agent`.

It reads the ticket, searches a knowledge pack with the summary and the description, and
cites the ids it got back in one `ticket_comment`, the way the research skill in
examples/ticket_rag/packs/ticket_rag/skills/research/ asks. It uses the MCP SDK only and
starts the two servers it needs from REPLAYBOOK_MCP_CONFIG over stdio. Which server, search
tool and id field it uses comes from the environment, so it works with any knowledge pack:

    export RESEARCH_SERVER=ticket_rag RESEARCH_TOOL=search_tickets RESEARCH_ID=key
    replaybook knowledge eval --agent external --type ticket_research \
        --command "python examples/ticket_rag/research_agent.py" ...
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main() -> None:
    config = json.loads(open(os.environ["REPLAYBOOK_MCP_CONFIG"], encoding="utf-8").read())
    server = os.environ.get("RESEARCH_SERVER", "ticket_rag")
    tool = os.environ.get("RESEARCH_TOOL", "search_tickets")
    id_field = os.environ.get("RESEARCH_ID", "key")
    async with AsyncExitStack() as stack:
        sessions: dict[str, Any] = {}

        async def call(name: str, tool_name: str, arguments: dict[str, Any]) -> Any:
            if name not in sessions:
                spec = config["mcpServers"][name]
                params = StdioServerParameters(
                    command=spec["command"], args=spec["args"], env=spec.get("env")
                )
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                sessions[name] = session
            return (await sessions[name].call_tool(tool_name, arguments)).structured_content

        ticket = await call("ticket", "ticket_get", {})
        query = f"{ticket['summary']}\n{ticket['description']}"[:1000]
        found = await call(server, tool, {"query": query})
        ids = [str(r[id_field]) for r in (found or {}).get("results", []) if id_field in r]
        body = "Related: " + (", ".join(ids[:10]) if ids else "none")
        await call("ticket", "ticket_comment", {"body": body})
        print(body)


if __name__ == "__main__":
    asyncio.run(main())
