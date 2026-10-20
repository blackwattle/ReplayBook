"""`replaybook knowledge import` and `replaybook knowledge check`: make a knowledge pack from
a team's own past-ticket search server, and prove it fits before anyone replays with it.

`import` lists the server's tools (the only call it makes) and writes a pack: tools.json
with the read-only tools and a pack.yaml whose `knowledge:` section is guessed from the
argument names and marked CHANGE_ME where only a person can tell (which field holds the
ticket id, the creation time, the resolution). Write tools are left out: a knowledge pack
never writes.

`check` validates the section against tools.json (every named argument exists, every tool is
read-only), optionally refreshes tools.json from the server, and runs one search through
the guard at a horizon: how many results the server returned, how many the guard removed
and why, and the fields of the first raw result, so CHANGE_ME can be filled in.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from replaybook.mock_env.generic import annotation
from replaybook.mock_env.guarded import KnowledgeSpec, ServerSpec, get_path

PLACEHOLDER = "CHANGE_ME"
QUERY_ARGS = ("query", "q", "text", "question", "search", "problem", "prompt")
LIMIT_ARGS = ("top_k", "limit", "k", "max_results", "n", "size", "count")
AS_OF_ARGS = ("created_before", "before", "as_of", "until", "created_until", "end_date")
TICKET_ARGS = re.compile(r"(^|_)(key|id|ticket|issue|number)($|_)")


def _properties(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    return dict(schema.get("properties") or {})


def _first(names: tuple[str, ...], properties: dict[str, Any]) -> str | None:
    return next((n for n in names if n in properties), None)


def skeleton(tool: dict[str, Any]) -> dict[str, Any]:
    """A guess at one tool's guard section from its argument names."""
    properties = _properties(tool)
    query = _first(QUERY_ARGS, properties)
    ticket = None if query else next((n for n in properties if TICKET_ARGS.search(n)), None)
    entry: dict[str, Any] = {}
    if ticket:
        entry["ticket_arg"] = ticket
    if query:
        entry["query_arg"] = query
    if ticket and not query and not _first(LIMIT_ARGS, properties):
        entry["result"] = PLACEHOLDER
    else:
        entry["results"] = PLACEHOLDER
    entry |= {
        "id": PLACEHOLDER,
        "created": PLACEHOLDER,
        "resolved": PLACEHOLDER,
        "until_resolved": [],
        "remove": [],
    }
    limit = _first(LIMIT_ARGS, properties)
    if limit:
        entry["limit_arg"] = limit
        maximum = (properties[limit] or {}).get("maximum")
        entry["max_limit"] = int(maximum) if isinstance(maximum, int | float) else 25
    as_of = _first(AS_OF_ARGS, properties)
    if as_of:
        entry["as_of_arg"] = as_of
        entry["as_of_format"] = "datetime"
    return entry


def manifest(
    name: str, description: str, server: dict[str, Any], tools: list[dict[str, Any]]
) -> dict[str, Any]:
    entries = {str(t["name"]): skeleton(t) for t in tools}
    search = next((n for n, e in entries.items() if "query_arg" in e), None)
    lookup = next((n for n, e in entries.items() if "result" in e and "ticket_arg" in e), None)
    knowledge: dict[str, Any] = {"server": server}
    if search:
        knowledge["search"] = search
    if lookup:
        knowledge["lookup"] = lookup
    knowledge["tools"] = entries
    return {
        "name": name,
        "description": description or f"Past tickets from the {name} search server",
        "knowledge": knowledge,
    }


HEADER = """# Knowledge pack: {name}. Its tools come from your own past-ticket search server, behind
# Replaybook's time-travel guard (docs/knowledge-sources.md). Fill in every CHANGE_ME:
#   results / result   the key holding the list of tickets / the single ticket
#   id, created        the fields with the ticket id and its creation time
#   resolved           the resolution time; until_resolved lists the fields that only
#                      exist once a ticket is resolved (resolution text, final status)
#   remove             fields that reveal the present (age in days, ranking scores)
#   timed              lists inside a ticket and their time field, e.g. {{comments: created}}
# then run: replaybook knowledge check {name} --query "a typical problem"
# Secrets go in .env and are referenced here as ${{NAME}}.
"""


def server_section(
    url: str = "", command: str = "", headers: dict[str, str] | None = None
) -> dict[str, Any]:
    import shlex

    # A stdio server starts in the pack directory, so paths given relative to where the
    # import ran are made absolute.
    parts = [str(Path(p).resolve()) if p and Path(p).exists() else p for p in shlex.split(command)]
    section: dict[str, Any] = {"url": url} if url else {"command": parts}
    if headers:
        section["headers"] = dict(headers)
    section["timeout_s"] = 60
    return section


def list_server_tools(server: dict[str, Any], base: Path) -> list[dict[str, Any]]:
    from replaybook.mock_env.remote import RemoteServer

    remote = RemoteServer(ServerSpec.parse(server, base))
    try:
        return remote.list_tools()
    finally:
        remote.close()


def import_knowledge(
    name: str,
    out: Path,
    url: str = "",
    command: str = "",
    json_path: Path | None = None,
    headers: dict[str, str] | None = None,
    description: str = "",
) -> dict[str, Any]:
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("name must be letters, digits, _ or -")
    if sum(bool(x) for x in (url, command, json_path)) != 1:
        raise ValueError("give exactly one of --url, --command or --json")
    pack_dir = Path(out) / name
    if (pack_dir / "pack.yaml").exists():
        raise FileExistsError(f"{pack_dir / 'pack.yaml'} exists; remove it or pick another name")
    pack_dir.mkdir(parents=True, exist_ok=True)
    if json_path is not None:
        from replaybook.mock_env.generic import load_tools

        listed = load_tools(json_path)
        server = server_section(url="${" + name.upper().replace("-", "_") + "_URL}")
    else:
        server = server_section(url, command, headers)
        listed = list_server_tools(server, pack_dir)
    reads = [t for t in listed if annotation(t, "readOnlyHint")]
    skipped = sorted(str(t["name"]) for t in listed if not annotation(t, "readOnlyHint"))
    (pack_dir / "tools.json").write_text(json.dumps({"tools": reads}, indent=2), encoding="utf-8")
    text = HEADER.format(name=name) + yaml.safe_dump(
        manifest(name, description, server, reads), sort_keys=False, allow_unicode=True
    )
    (pack_dir / "pack.yaml").write_text(text, encoding="utf-8")
    return {"pack": pack_dir, "tools": len(reads), "skipped": skipped, "listed": len(listed)}


def _placeholders(data: Any, path: str = "") -> list[str]:
    if isinstance(data, dict):
        return [p for k, v in data.items() for p in _placeholders(v, f"{path}.{k}".strip("."))]
    if isinstance(data, list):
        return [p for i, v in enumerate(data) for p in _placeholders(v, f"{path}[{i}]")]
    return [path] if data == PLACEHOLDER else []


def check_pack(
    pack: Any, refresh: bool = False, query: str = "", at: datetime | None = None, k: int = 5
) -> dict[str, Any]:
    """Problems with a knowledge pack, and the result of one guarded search."""
    from replaybook.mock_env.guarded import GuardedSource, source_for
    from replaybook.mock_env.remote import RemoteServer

    spec: KnowledgeSpec = pack.knowledge
    report: dict[str, Any] = {
        "pack": pack.name,
        "server": spec.server.describe(),
        "transport": spec.server.transport,
        "problems": [],
    }
    placeholders = _placeholders(pack.manifest.get("knowledge"))
    report["problems"] += [f"{p} is still {PLACEHOLDER}" for p in placeholders]
    if refresh:
        listed = list_server_tools(pack.manifest["knowledge"]["server"], pack.path)
        reads = [t for t in listed if annotation(t, "readOnlyHint")]
        (pack.path / "tools.json").write_text(
            json.dumps({"tools": reads}, indent=2), encoding="utf-8"
        )
        report["refreshed_tools"] = len(reads)
    if pack.tools_json is None:
        report["problems"].append("no tools.json: run with --refresh")
        return report
    from replaybook.mock_env.generic import load_tools

    tools = load_tools(pack.tools_json)
    report["problems"] += spec.check_tools(tools)
    if not query:
        return report
    if spec.search is None:
        report["problems"].append("knowledge.search names no tool, so nothing to probe")
        return report
    if report["problems"]:
        return report
    horizon = at or datetime.now(UTC)
    source: GuardedSource = source_for(pack)
    search = spec.tools[spec.search]
    assert search.query_arg is not None
    raw_args: dict[str, Any] = {search.query_arg: query}
    if search.limit_arg:
        raw_args[search.limit_arg] = search.max_limit or k
    remote: RemoteServer = source.remote
    raw = remote.call(search.name, raw_args)
    items = get_path(raw, search.results or "") if search.results else None
    first = items[0] if isinstance(items, list) and items else None
    report["raw_results"] = len(items) if isinstance(items, list) else None
    report["raw_first_fields"] = sorted(first) if isinstance(first, dict) else None
    if isinstance(raw, dict) and raw.get("error"):
        report["problems"].append(f"the server answered with an error: {raw['error']}")
        return report
    found, stats = source.search(query, k, horizon, None)
    report["horizon"] = horizon.isoformat()
    report["guarded_results"] = len(found)
    report["guard"] = stats.as_dict()
    report["first_guarded"] = found[0] if found else None
    if items and not found and stats.undated == stats.returned_by_server:
        report["problems"].append(
            f"every result was dropped as undated: check that '{search.id}' and "
            f"'{search.created}' are fields of each result (raw fields: "
            f"{', '.join(report['raw_first_fields'] or [])})"
        )
    return report
