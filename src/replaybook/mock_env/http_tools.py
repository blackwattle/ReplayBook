"""The function-calling door: the tool list as JSON Schema and a local HTTP endpoint per tool.

Frameworks that use a provider's native function calling instead of MCP get the same
recorded tools two ways. `replaybook tools schema` prints the tool list in the common
`{"type": "function", "function": {name, description, parameters}}` shape (or the raw MCP
shape), ready to hand to a model. `replaybook tool-endpoint` serves it:

    GET  /health              -> {"status": "ok", "tools": <count>}
    GET  /tools?shape=openai  -> the same list (shape: openai or mcp)
    POST /tools/<name>        -> body {"arguments": {...}} or the arguments themselves;
                                 answers {"result": ...}; 404 for an unknown tool

Every call goes through the same recording wrapper as an MCP call, so the record file,
the world state and the time-travel guard are shared with the other doors.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from replaybook.mock_env.registry import MockEnvironment
from replaybook.mock_env.serve import READY_PREFIX, announce_stdout, listening_socket

SHAPES = ("openai", "mcp")


def function_schemas(
    described: list[dict[str, Any]], shape: str = "openai"
) -> list[dict[str, Any]]:
    """The tool surface in the requested shape. `described` is what MockEnvironment
    .describe_tools() returns (name, description, input_schema, annotations)."""
    if shape not in SHAPES:
        raise ValueError(f"shape must be one of {', '.join(SHAPES)}")
    if shape == "mcp":
        return [
            {
                "name": t["name"],
                "description": t["description"] or "",
                "inputSchema": t["input_schema"],
                "annotations": t["annotations"],
            }
            for t in described
        ]
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"] or "",
                "parameters": t["input_schema"],
            },
        }
        for t in described
    ]


def tools_app(env: MockEnvironment) -> Any:
    """A Starlette app over one mock environment."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    described = env.describe_tools()

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "tools": len(described)})

    async def list_tools(request: Request) -> JSONResponse:
        shape = request.query_params.get("shape", "openai")
        try:
            return JSONResponse({"tools": function_schemas(described, shape)})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def call_tool(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in env.tools:
            return JSONResponse({"error": f"unknown tool '{name}'"}, status_code=404)
        body = await request.body()
        try:
            payload = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError as exc:
            return JSONResponse({"error": f"body is not JSON: {exc}"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        arguments = payload.get("arguments", payload)
        if not isinstance(arguments, dict):
            return JSONResponse({"error": "arguments must be a JSON object"}, status_code=400)
        result = env.call(name, arguments)
        status = 400 if isinstance(result, dict) and set(result) == {"error"} else 200
        return JSONResponse({"result": result}, status_code=status)

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/tools", list_tools, methods=["GET"]),
            Route("/tools/{name}", call_tool, methods=["POST"]),
        ]
    )


def serve_tools(
    env: MockEnvironment,
    host: str = "127.0.0.1",
    port: int = 0,
    announce: Callable[[str], None] = announce_stdout,
) -> None:
    """Serve the function-calling endpoint until killed. Port 0 takes a free port; the URL
    is announced on stdout as `REPLAYBOOK_LISTENING http://host:port` before serving."""
    import uvicorn

    sock = listening_socket(host, port)
    bound = sock.getsockname()[1]
    announce(f"{READY_PREFIX}http://{host}:{bound}")
    config = uvicorn.Config(
        tools_app(env), host=host, port=bound, log_level="warning", access_log=False
    )
    uvicorn.Server(config).run(sockets=[sock])


def write_schema(described: list[dict[str, Any]], shape: str, out: Path | None) -> str:
    text = json.dumps(function_schemas(described, shape), indent=2) + "\n"
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
    return text
