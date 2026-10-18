"""`replaybook tools import`: turn any MCP server's tool list into a pack.

The tool list (names, descriptions, input schemas, annotations) comes from a server
started over stdio, from a server already running over streamable HTTP (a URL), or from a
JSON file saved earlier. The pack that comes out has:

- tools.json   the tool list, which the generic recording mock serves (reads answer from
               canned responses in the world slice, writes are recorded, nothing executes)
- pack.yaml    name, description and an empty request-types section with an example
- rules.yaml   a scoring skeleton: every write tool is its own family, its required
               arguments are the target and every argument matters; tools the server
               annotates as destructive are critical. A person edits this.
- skills/example/SKILL.md
               a skill skeleton listing the non-destructive tools, with a placeholder
               verification check for every write that fails `replaybook skills validate`
               until a person names the read tool and the state it must show.

Nothing here contacts the real system behind the server; listing tools is the only call.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any

import yaml

from replaybook.mock_env.generic import annotation, load_tools


def list_tools_from_mcp(
    command: list[str], env: dict[str, str] | None = None, timeout_s: float = 60.0
) -> list[dict[str, Any]]:
    """Start an MCP server over stdio, list its tools, stop it."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    if not command:
        raise ValueError("command must not be empty")
    params = StdioServerParameters(
        command=command[0], args=list(command[1:]), env={**os.environ, **(env or {})}
    )

    async def run() -> list[dict[str, Any]]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": t.input_schema,
                        "annotations": t.annotations.model_dump(exclude_none=True)
                        if t.annotations
                        else {},
                    }
                    for t in listed.tools
                ]

    async def with_timeout() -> list[dict[str, Any]]:
        return await asyncio.wait_for(run(), timeout=timeout_s)

    return asyncio.run(with_timeout())


def list_tools_from_url(url: str, timeout_s: float = 60.0) -> list[dict[str, Any]]:
    """Connect to an MCP server over streamable HTTP, list its tools, disconnect. In
    air-gapped mode the host must be in REPLAYBOOK_ALLOWED_HOSTS."""
    from mcp.client.client import Client

    async def run() -> list[dict[str, Any]]:
        async with Client(url) as client:
            listed = await client.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.input_schema,
                    "annotations": t.annotations.model_dump(exclude_none=True)
                    if t.annotations
                    else {},
                }
                for t in listed.tools
            ]

    async def with_timeout() -> list[dict[str, Any]]:
        return await asyncio.wait_for(run(), timeout=timeout_s)

    return asyncio.run(with_timeout())


def list_tools_from_json(path: Path) -> list[dict[str, Any]]:
    """A saved tool list: either `{"tools": [...]}` (the MCP tools/list result) or a list."""
    tools = load_tools(path)
    cleaned: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            raise ValueError(f"{path}: every tool needs a name")
        cleaned.append(
            {
                "name": str(tool["name"]),
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("inputSchema") or tool.get("input_schema") or {},
                "annotations": tool.get("annotations") or {},
            }
        )
    return cleaned


def rules_skeleton(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Scoring rules a person should review: generous defaults that compare everything."""
    rules: dict[str, Any] = {}
    unsafe: dict[str, Any] = {}
    for tool in sorted(tools, key=lambda t: t["name"]):
        if annotation(tool, "readOnlyHint"):
            continue
        schema = tool.get("inputSchema") or {}
        properties = list((schema.get("properties") or {}).keys())
        required = [p for p in (schema.get("required") or []) if p in properties]
        rules[tool["name"]] = {
            "family": tool["name"],
            "target": required or properties[:1],
            "matters": properties,
        }
        if annotation(tool, "destructiveHint"):
            unsafe[tool["name"]] = {
                "severity": "critical",
                "reason": "annotated destructive by the server; review",
            }
    return {"tools": rules, "unsafe": unsafe, "superseded": {}}


RULES_HEADER = """# Scoring rules generated by `replaybook tools import`. Review and edit:
#
# tools:      how two calls of one tool are compared. Every write tool starts as its own
#             family with its required arguments as the target and every argument
#             mattering. Put tools that do the same job in different ways (a legacy and a
#             current one) in one family. Drop arguments that do not matter from
#             `matters`, add `defaults` for arguments agents may omit, list arguments that
#             are sets under `unordered`, case-sensitive ones under `keep_case`, and map an
#             argument to a core value normaliser under `equivalents` (duration_ms).
# unsafe:     tools that are dangerous whatever the arguments. Tools the server annotated
#             as destructive are listed as critical; add others, or lower a severity to
#             high, medium or low. Argument-based checks go in safety.py.
# superseded: tools from before a procedure change, with a label for the failure groups.
"""

PACK_HEADER = """# Pack generated by `replaybook tools import`. Every tool here is served by a
# recording mock built from tools.json: reads answer from `state["reads"][<tool>]` in the
# case's world slice (or an empty object), writes are recorded and never executed.
#
# Fill in request_types so tickets can be typed and matched to this pack:
#
#   request_types:
#     pay_rate_change:
#       label: Pay rate change
#       keywords: [pay rate, hourly rate, salary]
#       strong_keywords: [pay rate]
#       components: [payroll]
#       tools: [payroll_set_pay_rate]      # recorded actions with these tools type a ticket
#       skill: skills/pay_rate_change/SKILL.md
"""


def write_pack(
    out_dir: Path, name: str, tools: list[dict[str, Any]], description: str = ""
) -> Path:
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"pack name '{name}' must be letters, digits, _ or -")
    if not tools:
        raise ValueError("the server listed no tools")
    pack = Path(out_dir) / name
    if pack.exists():
        raise FileExistsError(f"{pack} already exists; remove it or choose another name")
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text(
        f'"""{name} pack, generated by replaybook tools import. Every tool is a local mock."""\n',
        encoding="utf-8",
    )
    (pack / "tools.json").write_text(
        json.dumps({"tools": sorted(tools, key=lambda t: t["name"])}, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "name": name,
        "description": description or f"Recording mock of the {name} tools (imported)",
        "request_types": {},
    }
    (pack / "pack.yaml").write_text(
        PACK_HEADER + yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (pack / "rules.yaml").write_text(
        RULES_HEADER + yaml.safe_dump(rules_skeleton(tools), sort_keys=False), encoding="utf-8"
    )
    (pack / "skills" / "example").mkdir(parents=True)
    (pack / "skills" / "example" / "SKILL.md").write_text(example_skill(name, tools), "utf-8")
    return pack


def example_skill(name: str, tools: list[dict[str, Any]]) -> str:
    """A SKILL.md skeleton. Every write gets a placeholder check (an unknown read tool and
    an empty expect) so validation fails until a person writes the real one."""
    usable = [
        t for t in sorted(tools, key=lambda t: t["name"]) if not annotation(t, "destructiveHint")
    ]
    writes = [t for t in usable if not annotation(t, "readOnlyHint")]
    listed = [t["name"] for t in usable] + [
        "ticket_ask_requester",
        "ticket_escalate",
        "ticket_comment",
    ]
    lines = [
        "---",
        f"name: {name}-example",
        "description: TODO - what this skill does, in one line.",
        "request_type: TODO_request_type",
        f"tools: {', '.join(listed)}",
        "verify:",
    ]
    for tool in writes:
        schema = tool.get("inputSchema") or {}
        required = [
            p for p in (schema.get("required") or []) if p in (schema.get("properties") or {})
        ]
        args = ", ".join(f'{arg}: "{{{arg}}}"' for arg in required)
        lines += [
            f"  {tool['name']}:",
            f"    tool: TODO_read_tool_that_shows_{tool['name']}",
            f"    args: {{{args}}}",
            "    expect: {}",
            "    timeout_s: 30",
        ]
    if not writes:
        lines[-1] = "verify: {}"
    lines += [
        "---",
        "",
        f"# TODO: a {name} request this skill handles",
        "",
        "Generated by `replaybook tools import`. Destructive tools are left out of `tools:` on",
        "purpose. Every write above has a placeholder check that fails `replaybook skills",
        "validate` until you fill it in: the read-only tool that shows the write's effect, the",
        "arguments it needs and the state it must show (`expect`), with {argument} templates",
        "taken from the write. Then name this file under `skill:` for the request type in",
        "pack.yaml.",
        "",
        "## When this applies",
        "",
        "## Information you need",
        "If something you need is missing, ask the requester with `ticket_ask_requester` and stop.",
        "",
        "## When to hand back",
        "Call `ticket_escalate` with a one-line reason and stop when the ticket needs a decision",
        "a person should make.",
        "",
        "## Procedure",
        "",
        "## Never",
        "",
    ]
    return "\n".join(lines)


def import_tools(
    name: str,
    out_dir: Path,
    command: str = "",
    json_path: Path | None = None,
    description: str = "",
    url: str = "",
) -> dict[str, Any]:
    """CLI helper: list the tools from one source and write the pack."""
    sources = sum([bool(command), json_path is not None, bool(url)])
    if sources != 1:
        raise ValueError("give exactly one of --command, --json or --url")
    if command:
        tools = list_tools_from_mcp(shlex.split(command))
        source = f"mcp: {command}"
    elif url:
        tools = list_tools_from_url(url)
        source = f"mcp over http: {url}"
    else:
        assert json_path is not None
        tools = list_tools_from_json(json_path)
        source = f"json: {json_path}"
    pack = write_pack(out_dir, name, tools, description)
    reads = sum(annotation(t, "readOnlyHint") for t in tools)
    destructive = sum(annotation(t, "destructiveHint") for t in tools)
    return {
        "pack": pack,
        "source": source,
        "tools": len(tools),
        "reads": reads,
        "writes": len(tools) - reads,
        "destructive": destructive,
    }
