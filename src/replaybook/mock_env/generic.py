"""A recording mock built from an MCP tool list, for packs that `replaybook tools import`
generated and nobody has written a server.py for yet.

Each tool in tools.json becomes a function with the schema's parameters. Reads answer
from the pack's world slice: `state["reads"][<tool>]` if present, otherwise an empty
object. Writes are recorded and answer with the arguments they were given. Nothing is
ever executed. Tools annotated read-only become reads, tools annotated destructive are
flagged destructive, everything else is an ordinary write.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any

from replaybook.mock_env.base import MockServer

if TYPE_CHECKING:
    from replaybook.mock_env.recorder import CallRecorder
    from replaybook.mock_env.world import WorldState
    from replaybook.packs import Pack

JSON_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def load_tools(path: Any) -> list[dict[str, Any]]:
    data = json.loads(open(path, encoding="utf-8").read())
    tools = data["tools"] if isinstance(data, dict) and "tools" in data else data
    if not isinstance(tools, list):
        raise ValueError(f"{path}: expected a list of tools or {{'tools': [...]}}")
    return tools


def annotation(tool: dict[str, Any], key: str) -> bool:
    """Read an MCP annotation in either spelling (readOnlyHint or read_only_hint)."""
    annotations = tool.get("annotations") or {}
    camel = key
    snake = "".join("_" + c.lower() if c.isupper() else c for c in key)
    return bool(annotations.get(camel, annotations.get(snake, False)))


def parameters_of(tool: dict[str, Any]) -> list[inspect.Parameter]:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    params: list[inspect.Parameter] = []
    for name, spec in properties.items():
        spec = spec or {}
        kind = JSON_TYPES.get(str(spec.get("type", "")), Any)
        if name in required:
            params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=kind))
        else:
            params.append(
                inspect.Parameter(
                    name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=kind | None
                )
            )
    return params


def make_tool(tool: dict[str, Any], state: dict[str, Any], read: bool) -> Any:
    name = str(tool["name"])
    params = parameters_of(tool)

    def fn(**kwargs: Any) -> dict[str, Any]:
        if read:
            canned = (state.get("reads") or {}).get(name)
            return dict(canned) if isinstance(canned, dict) else {}
        return {"recorded": True, **{k: v for k, v in kwargs.items() if v is not None}}

    fn.__name__ = name
    fn.__qualname__ = name
    fn.__doc__ = str(tool.get("description") or f"{name} (recording mock)")
    fn.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict[str, Any]}
    return fn


def build_recording_server(pack: Pack, world: WorldState, recorder: CallRecorder) -> MockServer:
    tools = load_tools(pack.tools_json)
    description = pack.description or f"Recording mock of the {pack.name} tools"
    server = MockServer(
        pack.name,
        f"{description}. Reads answer from the case's world state; writes are recorded and "
        "never executed.",
        world,
        recorder,
    )
    state = world.slice(pack.name)
    for tool in tools:
        read = annotation(tool, "readOnlyHint")
        fn = make_tool(tool, state, read)
        if read:
            server.read(fn)
        else:
            server.write(
                destructive=annotation(tool, "destructiveHint"),
                idempotent=annotation(tool, "idempotentHint"),
            )(fn)
    return server
