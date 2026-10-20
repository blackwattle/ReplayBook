"""Knowledge packs: a team's own retrieval server (a RAG over past tickets) behind the
time-travel guard.

Replaybook's own knowledge tools answer from the imported dataset. A team that already has
a retrieval MCP server wants the agent under test to use that, with its real tool names,
descriptions and ranking. During replay that server is dangerous: its index holds the very
ticket being replayed, its resolution, and everything that happened afterwards. A
knowledge pack puts the server behind the same rule as the built-in tools:

- only tickets created strictly before the ticket under test come back, never that ticket;
- fields that only exist once a ticket is resolved (resolution text, final status) are
  removed unless it was resolved before the horizon;
- timestamped lists inside a ticket (comments) are cut at the horizon;
- fields that reveal the present (age in days, ranking details) are removed;
- a tool that takes a ticket id answers only for a visible ticket;
- results are over-fetched and trimmed, so filtering does not leave the agent short; and
  when the server has an "as of" argument it is set to the horizon and hidden from the agent.

Anything the guard cannot place in time (no id, no parseable creation time) is dropped:
the guard fails closed. Only tools the server annotates read-only, or the pack explicitly
marks read-only, can be exposed; a knowledge pack never writes.

A pack becomes a knowledge pack with a `knowledge:` section in pack.yaml and the server's
tool list in tools.json (`replaybook knowledge import` writes both):

    knowledge:
      server:
        url: ${TICKET_RAG_URL}                 # streamable HTTP, or
        command: [python, server.py]           # stdio, started in the pack directory
        headers: {Authorization: "Bearer ${TICKET_RAG_TOKEN}"}
        env: {}                                # extra environment for a stdio server
        timeout_s: 60
      search: search_tickets                   # the tool `replaybook knowledge eval` scores
      lookup: get_ticket                       # tells whether a ticket named by an id is visible
      tools:
        search_tickets:
          results: results                     # the key that holds the list of tickets
          id: issue_key
          created: created
          resolved: resolved
          until_resolved: [how_it_ended, status]
          remove: [age_days, ranking]
          query_arg: query
          limit_arg: top_k
          max_limit: 25
          as_of_arg: created_before            # optional; hidden from the agent
          as_of_format: datetime               # or date (YYYY-MM-DD)
          count_field: count
        get_ticket:
          ticket_arg: issue_key
          result: ticket                       # where the single ticket is ("" = the result)
          id: issue_key
          created: created
          resolved: resolved
          until_resolved: [resolution_summary, status]
          timed: {comments: created}
          remove: [links, updated]

A live index changes every day, so two runs can see different answers. `cache:` pins them:

      cache: off        # the default: every call goes to the server, nothing is written
      cache: record     # reuse a stored answer, otherwise ask the server and store it
      cache: replay     # stored answers only; a call never stored is an error
      cache: {mode: record, dir: .cache}   # dir is relative to the pack (default .cache)

The cache holds the server's raw answers, so the guard runs on every read and a cached
answer can never leak more than a live one. REPLAYBOOK_KNOWLEDGE_CACHE overrides the mode
and REPLAYBOOK_KNOWLEDGE_CACHE_DIR the directory (a subdirectory per pack). `off` is the
default because the cache is raw ticket text on disk, which a team should choose to keep.

`docs/knowledge-sources.md` explains each field. Values may name environment variables as
${NAME}; secrets belong in `.env`, never in pack.yaml.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from replaybook.mock_env.base import MockServer, ToolError
from replaybook.mock_env.generic import annotation, load_tools, parameters_of

if TYPE_CHECKING:
    from replaybook.mock_env.recorder import CallRecorder
    from replaybook.mock_env.world import WorldState
    from replaybook.packs import Pack

ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
AS_OF_FORMATS = ("datetime", "date")
CACHE_MODES = ("off", "record", "replay")
CACHE_MODE_ENV = "REPLAYBOOK_KNOWLEDGE_CACHE"
CACHE_DIR_ENV = "REPLAYBOOK_KNOWLEDGE_CACHE_DIR"
DEFAULT_LIMIT = 10
GUARD_NOTE = (
    "Only tickets created before the current ticket are visible, as they stood then; the "
    "current ticket itself is not in here."
)


class KnowledgeSpecError(ValueError):
    """The knowledge section of a pack does not fit its tools."""


# --- the spec ----------------------------------------------------------------------------


def expand_env(value: str, environ: dict[str, str] | None = None) -> str:
    """Replace ${NAME} with the environment variable; a missing one is an error, so a
    forgotten token fails loudly instead of sending an empty header."""
    env = os.environ if environ is None else environ

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise KnowledgeSpecError(f"environment variable {name} is not set (put it in .env)")
        return env[name]

    return ENV_REF.sub(sub, value)


@dataclass(frozen=True)
class ServerSpec:
    """How to reach the retrieval server: a URL (streamable HTTP) or a command (stdio)."""

    url: str = ""
    command: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    timeout_s: float = 60.0

    @classmethod
    def parse(cls, data: Any, base: Path) -> ServerSpec:
        if not isinstance(data, dict):
            raise KnowledgeSpecError("knowledge.server must be a mapping with url or command")
        url = str(data.get("url") or "")
        command = data.get("command") or ()
        if isinstance(command, str):
            command = (command,)
        if bool(url) == bool(command):
            raise KnowledgeSpecError("knowledge.server needs exactly one of url or command")
        return cls(
            url=url,
            command=tuple(str(c) for c in command),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            cwd=base,
            timeout_s=float(data.get("timeout_s") or 60.0),
        )

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def describe(self) -> str:
        """Where the server is, without header values or environment (they carry secrets)."""
        if self.url:
            return self.url.split("?", 1)[0]
        return " ".join(self.command)


@dataclass(frozen=True)
class CacheSpec:
    """Whether and where the server's raw answers are kept (see the module docstring)."""

    mode: str = "off"
    dir: Path | None = None

    @classmethod
    def parse(cls, data: Any, base: Path) -> CacheSpec:
        # YAML reads a bare `off` as false and `on` as true.
        if isinstance(data, dict):
            mode, directory = data.get("mode"), data.get("dir")
        elif data is None or isinstance(data, str | bool):
            mode, directory = data, None
        else:
            raise KnowledgeSpecError("knowledge.cache must be a mode or {mode, dir}")
        if mode is None or mode is False:
            mode = "off"
        elif mode is True:
            mode = "record"
        mode = str(mode)
        if mode not in CACHE_MODES:
            raise KnowledgeSpecError(f"knowledge.cache must be one of {', '.join(CACHE_MODES)}")
        return cls(mode, (base / str(directory)) if directory else base / ".cache")

    def effective(self, pack_name: str) -> CacheSpec:
        """This spec with the environment's overrides applied."""
        mode = os.environ.get(CACHE_MODE_ENV, "").strip() or self.mode
        if mode not in CACHE_MODES:
            raise KnowledgeSpecError(f"{CACHE_MODE_ENV} must be one of {', '.join(CACHE_MODES)}")
        root = os.environ.get(CACHE_DIR_ENV, "").strip()
        directory = Path(root).expanduser() / pack_name if root else self.dir
        return CacheSpec(mode, directory.resolve() if directory else None)


class ResponseCache:
    """The server's raw answers on disk, one JSON file per (server, tool, arguments)."""

    def __init__(self, spec: CacheSpec, server: str) -> None:
        self.mode = spec.mode
        self.dir = spec.dir
        self.server = server

    def key(self, tool: str, args: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"server": self.server, "tool": tool, "args": args}, sort_keys=True, default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        assert self.dir is not None
        return self.dir / key[:2] / f"{key}.json"

    def get(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        if self.mode == "off" or self.dir is None:
            return None
        path = self._path(self.key(tool, args))
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["response"]

    def put(self, tool: str, args: dict[str, Any], response: dict[str, Any]) -> None:
        if self.mode != "record" or self.dir is None:
            return
        path = self._path(self.key(tool, args))
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"tool": tool, "args": args, "response": response}
        temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(record, sort_keys=True, default=str), encoding="utf-8")
        os.replace(temporary, path)  # atomic: parallel mock servers may record the same call


def cache_digest(directory: Path | None) -> str | None:
    """SHA-256 over every stored answer (names and contents), or None without a cache."""
    if directory is None or not directory.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.json")):
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class ToolSpec:
    """Where one tool's tickets are and how to guard them."""

    name: str
    results: str | None = None
    result: str | None = None
    id: str = "id"
    created: str = "created"
    resolved: str | None = None
    until_resolved: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    timed: dict[str, str] = field(default_factory=dict)
    ticket_arg: str | None = None
    query_arg: str | None = None
    limit_arg: str | None = None
    max_limit: int | None = None
    as_of_arg: str | None = None
    as_of_format: str = "datetime"
    count_field: str | None = None
    read_only: bool = False

    @classmethod
    def parse(cls, name: str, data: Any) -> ToolSpec:
        data = data or {}
        if not isinstance(data, dict):
            raise KnowledgeSpecError(f"knowledge.tools.{name} must be a mapping")
        has_results, has_result = "results" in data, "result" in data
        if has_results == has_result:
            raise KnowledgeSpecError(
                f"knowledge.tools.{name} needs exactly one of results (a list of tickets) or "
                "result (one ticket)"
            )
        if has_results and not data.get("results"):
            raise KnowledgeSpecError(
                f"knowledge.tools.{name}.results must name the key that holds the list"
            )
        as_of_format = str(data.get("as_of_format") or "datetime")
        if as_of_format not in AS_OF_FORMATS:
            raise KnowledgeSpecError(
                f"knowledge.tools.{name}.as_of_format must be one of {', '.join(AS_OF_FORMATS)}"
            )
        timed = data.get("timed") or {}
        if not isinstance(timed, dict):
            raise KnowledgeSpecError(f"knowledge.tools.{name}.timed must map a list to its time")
        max_limit = data.get("max_limit")

        def text(key: str) -> str | None:
            value = data.get(key)
            return str(value) if value not in (None, "") else None

        return cls(
            name=name,
            results=str(data["results"]) if has_results else None,
            result=str(data.get("result") or "") if has_result else None,
            id=str(data.get("id") or "id"),
            created=str(data.get("created") or "created"),
            resolved=text("resolved"),
            until_resolved=tuple(str(f) for f in data.get("until_resolved") or ()),
            remove=tuple(str(f) for f in data.get("remove") or ()),
            timed={str(k): str(v) for k, v in timed.items()},
            ticket_arg=text("ticket_arg"),
            query_arg=text("query_arg"),
            limit_arg=text("limit_arg"),
            max_limit=int(max_limit) if max_limit is not None else None,
            as_of_arg=text("as_of_arg"),
            as_of_format=as_of_format,
            count_field=text("count_field"),
            read_only=bool(data.get("read_only", False)),
        )

    def hidden_args(self) -> set[str]:
        """Arguments the guard sets itself; the agent never sees them."""
        return {self.as_of_arg} if self.as_of_arg else set()


@dataclass(frozen=True)
class KnowledgeSpec:
    server: ServerSpec
    tools: dict[str, ToolSpec]
    search: str | None = None
    lookup: str | None = None
    cache: CacheSpec = field(default_factory=CacheSpec)

    @classmethod
    def parse(cls, data: Any, base: Path) -> KnowledgeSpec:
        if not isinstance(data, dict):
            raise KnowledgeSpecError("knowledge must be a mapping")
        tools_data = data.get("tools") or {}
        if not isinstance(tools_data, dict) or not tools_data:
            raise KnowledgeSpecError("knowledge.tools must list at least one tool")
        tools = {str(n): ToolSpec.parse(str(n), spec) for n, spec in tools_data.items()}
        search = str(data["search"]) if data.get("search") else None
        lookup = str(data["lookup"]) if data.get("lookup") else None
        for role, name in (("search", search), ("lookup", lookup)):
            if name is not None and name not in tools:
                raise KnowledgeSpecError(f"knowledge.{role} names '{name}', not in knowledge.tools")
        if lookup is not None and (tools[lookup].result is None or not tools[lookup].ticket_arg):
            raise KnowledgeSpecError(
                f"knowledge.lookup '{lookup}' must return one ticket (result:) for a ticket_arg"
            )
        if search is not None and not tools[search].query_arg:
            raise KnowledgeSpecError(f"knowledge.search '{search}' needs query_arg")
        needs_lookup = [t.name for t in tools.values() if t.ticket_arg and t.result is None]
        if needs_lookup and lookup is None:
            raise KnowledgeSpecError(
                f"{', '.join(needs_lookup)} take a ticket id; name a lookup tool "
                "(knowledge.lookup) so the guard can check that ticket is visible"
            )
        return cls(
            ServerSpec.parse(data.get("server"), base),
            tools,
            search,
            lookup,
            CacheSpec.parse(data.get("cache"), base),
        )

    def check_tools(self, listed: list[dict[str, Any]]) -> list[str]:
        """Problems between this spec and the server's tool list (empty when they fit)."""
        by_name = {str(t["name"]): t for t in listed}
        problems: list[str] = []
        for spec in self.tools.values():
            tool = by_name.get(spec.name)
            if tool is None:
                problems.append(f"{spec.name}: the server has no such tool")
                continue
            if not spec.read_only and not annotation(tool, "readOnlyHint"):
                problems.append(
                    f"{spec.name}: not annotated read-only by the server; set read_only: true "
                    "in the pack only if you know it never writes"
                )
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            properties = set((schema.get("properties") or {}).keys())
            for label, arg in (
                ("ticket_arg", spec.ticket_arg),
                ("query_arg", spec.query_arg),
                ("limit_arg", spec.limit_arg),
                ("as_of_arg", spec.as_of_arg),
            ):
                if arg and arg not in properties:
                    problems.append(f"{spec.name}: {label} '{arg}' is not an argument of the tool")
            required = set(schema.get("required") or ())
            if spec.as_of_arg and spec.as_of_arg in required:
                problems.append(f"{spec.name}: as_of_arg '{spec.as_of_arg}' must be optional")
        return problems


# --- the guard ---------------------------------------------------------------------------


@dataclass
class GuardStats:
    """What the guard removed from one call, for `knowledge eval` and `knowledge check`."""

    returned_by_server: int = 0
    kept: int = 0
    after_horizon: int = 0
    the_ticket_itself: int = 0
    undated: int = 0
    trimmed: int = 0
    fields_removed: int = 0
    items_cut: int = 0

    @property
    def leaks(self) -> int:
        """Results the server returned that the agent must not see."""
        return self.after_horizon + self.the_ticket_itself + self.undated

    def add(self, other: GuardStats) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__} | {
            "leaks": self.leaks
        }


def parse_when(value: Any) -> datetime | date | None:
    """A timestamp as the server wrote it: a datetime (naive is UTC), a bare date, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def before(value: Any, horizon: datetime) -> bool:
    """Strictly before the horizon. A bare date counts only when the whole day is before the
    horizon's day: the same day could be after it."""
    when = parse_when(value)
    if when is None:
        return False
    if isinstance(when, datetime):
        return when < horizon
    return when < horizon.astimezone(UTC).date()


def same_id(a: Any, b: Any) -> bool:
    return str(a).strip().casefold() == str(b).strip().casefold()


def get_path(data: Any, path: str) -> Any:
    """A dotted path into nested dicts; "" is the value itself; None when absent."""
    if not path:
        return data
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _drop(data: dict[str, Any], path: str) -> bool:
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.get(part)  # type: ignore[assignment]
        if not isinstance(data, dict):
            return False
    if parts[-1] in data:
        del data[parts[-1]]
        return True
    return False


def guard_ticket(
    spec: ToolSpec, ticket: Any, horizon: datetime, exclude: str | None, stats: GuardStats
) -> dict[str, Any] | None:
    """One ticket as the agent may see it at the horizon, or None when it must not be seen."""
    if not isinstance(ticket, dict):
        stats.undated += 1
        return None
    ticket_id = ticket.get(spec.id)
    if ticket_id in (None, ""):
        stats.undated += 1
        return None
    if exclude is not None and same_id(ticket_id, exclude):
        stats.the_ticket_itself += 1
        return None
    created = ticket.get(spec.created)
    if parse_when(created) is None:
        stats.undated += 1
        return None
    if not before(created, horizon):
        stats.after_horizon += 1
        return None
    shown = _deep_copy(ticket)
    resolved_in_time = spec.resolved is not None and before(shown.get(spec.resolved), horizon)
    removals = list(spec.remove)
    if not resolved_in_time:
        removals += list(spec.until_resolved)
        if spec.resolved is not None and spec.resolved in shown:
            removals.append(spec.resolved)
    for path in removals:
        if _drop(shown, path):
            stats.fields_removed += 1
    for list_path, time_field in spec.timed.items():
        items = get_path(shown, list_path)
        if isinstance(items, list):
            kept = [
                item
                for item in items
                if isinstance(item, dict) and before(item.get(time_field), horizon)
            ]
            stats.items_cut += len(items) - len(kept)
            items[:] = kept
    return shown


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def guard_result(
    spec: ToolSpec,
    result: Any,
    horizon: datetime,
    exclude: str | None,
    limit: int | None = None,
) -> tuple[dict[str, Any], GuardStats]:
    """A tool result as the agent may see it at the horizon, and what was removed."""
    stats = GuardStats()
    if not isinstance(result, dict):
        raise ToolError("the knowledge server returned something other than an object")
    if result.get("error") or result.get("ok") is False:
        message = result.get("error") or "the knowledge server reported an error"
        return {"error": str(message)}, stats
    if spec.results is not None:
        items = get_path(result, spec.results)
        if not isinstance(items, list):
            raise ToolError("the knowledge server returned no result list where the pack said")
        stats.returned_by_server = len(items)
        kept = [
            shown
            for item in items
            if (shown := guard_ticket(spec, item, horizon, exclude, stats)) is not None
        ]
        if limit is not None and len(kept) > limit:
            stats.trimmed = len(kept) - limit
            kept = kept[:limit]
        stats.kept = len(kept)
        shown_result = _deep_copy(result)
        _set_path(shown_result, spec.results, kept)
        if spec.count_field and spec.count_field in shown_result:
            shown_result[spec.count_field] = len(kept)
        shown_result["horizon"] = horizon.isoformat()
        return shown_result, stats
    ticket = get_path(result, spec.result or "")
    stats.returned_by_server = 1 if ticket is not None else 0
    shown_ticket = guard_ticket(spec, ticket, horizon, exclude, stats)
    if shown_ticket is None:
        raise ToolError("that ticket is not available")
    stats.kept = 1
    if spec.result:
        shown_result = _deep_copy(result)
        _set_path(shown_result, spec.result, shown_ticket)
    else:
        shown_result = shown_ticket
    shown_result["horizon"] = horizon.isoformat()
    return shown_result, stats


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value


def as_of_value(spec: ToolSpec, horizon: datetime) -> str:
    if spec.as_of_format == "date":
        return horizon.astimezone(UTC).date().isoformat()
    return horizon.isoformat()


def requested_limit(spec: ToolSpec, args: dict[str, Any], schema: dict[str, Any]) -> int | None:
    if not spec.limit_arg:
        return None
    given = args.get(spec.limit_arg)
    if given is None:
        given = ((schema.get("properties") or {}).get(spec.limit_arg) or {}).get("default")
    try:
        return max(1, int(given)) if given is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


# --- the source and its tools ------------------------------------------------------------


class GuardedSource:
    """A knowledge pack's server and spec: calls a tool with the guard around it."""

    def __init__(
        self,
        spec: KnowledgeSpec,
        tools: list[dict[str, Any]],
        remote: Any,
        cache: ResponseCache | None = None,
    ) -> None:
        self.spec = spec
        self.listed = {str(t["name"]): t for t in tools}
        self.remote = remote
        self.cache = cache or ResponseCache(CacheSpec(), spec.server.describe())

    def fetch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """The server's raw answer: from the cache when it has one, else from the server."""
        cached = self.cache.get(name, args)
        if cached is not None:
            return cached
        if self.cache.mode == "replay":
            raise ToolError(
                "no recorded answer for this call (knowledge cache in replay mode); record it "
                "first with cache: record"
            )
        response = self.remote.call(name, args)
        if not (response.get("error") or response.get("ok") is False):
            self.cache.put(name, args, response)  # errors are not kept: record retries them
        return response

    def schema(self, name: str) -> dict[str, Any]:
        tool = self.listed.get(name) or {}
        return tool.get("inputSchema") or tool.get("input_schema") or {}

    def call(
        self, name: str, args: dict[str, Any], horizon: datetime, exclude: str | None
    ) -> tuple[dict[str, Any], GuardStats]:
        spec = self.spec.tools[name]
        args = {k: v for k, v in args.items() if v is not None and k not in spec.hidden_args()}
        if spec.ticket_arg is not None and spec.ticket_arg in args:
            self._check_visible(str(args[spec.ticket_arg]), spec, horizon, exclude)
        limit = requested_limit(spec, args, self.schema(name))
        if spec.limit_arg and spec.max_limit:
            args[spec.limit_arg] = spec.max_limit
        if spec.as_of_arg:
            args[spec.as_of_arg] = as_of_value(spec, horizon)
        result = self.fetch(name, args)
        return guard_result(spec, result, horizon, exclude, limit)

    def _check_visible(
        self, ticket_id: str, spec: ToolSpec, horizon: datetime, exclude: str | None
    ) -> None:
        if exclude is not None and same_id(ticket_id, exclude):
            raise ToolError("that ticket is not available")
        if spec.result is not None:
            return  # a single-ticket tool is checked on its own result
        assert self.spec.lookup is not None  # KnowledgeSpec.parse insists
        lookup = self.spec.tools[self.spec.lookup]
        assert lookup.ticket_arg is not None
        found = self.fetch(lookup.name, {lookup.ticket_arg: ticket_id})
        guard_result(lookup, found, horizon, exclude)  # raises ToolError when not visible

    def search(
        self, query: str, limit: int, horizon: datetime, exclude: str | None
    ) -> tuple[list[dict[str, Any]], GuardStats]:
        """The search tool's tickets for `knowledge eval`, guarded, best first."""
        if self.spec.search is None:
            raise KnowledgeSpecError("the pack names no search tool (knowledge.search)")
        spec = self.spec.tools[self.spec.search]
        assert spec.query_arg is not None
        args: dict[str, Any] = {spec.query_arg: query}
        if spec.limit_arg:
            args[spec.limit_arg] = limit
        shown, stats = self.call(spec.name, args, horizon, exclude)
        if "error" in shown:
            raise ToolError(str(shown["error"]))
        return list(get_path(shown, spec.results or "results") or []), stats


def horizon_of(world: WorldState) -> tuple[datetime | None, str | None]:
    if world.ticket is None:
        return None, None
    created = parse_when(world.ticket.created)
    if not isinstance(created, datetime):
        return None, world.ticket.id
    return created, world.ticket.id


def _make_tool(source: GuardedSource, name: str, world: WorldState, description: str) -> Any:
    spec = source.spec.tools[name]
    tool = source.listed[name]
    params = [p for p in parameters_of(tool) if p.name not in spec.hidden_args()]

    def fn(**kwargs: Any) -> dict[str, Any]:
        horizon, exclude = horizon_of(world)
        if horizon is None:
            raise ToolError("no ticket is attached to this environment, so there is no horizon")
        try:
            shown, _stats = source.call(name, kwargs, horizon, exclude)
        except ToolError:
            raise
        except Exception as exc:  # the server is someone else's; the agent gets an error
            raise ToolError(f"the knowledge server failed: {type(exc).__name__}: {exc}") from exc
        return shown

    fn.__name__ = name
    fn.__qualname__ = name
    fn.__doc__ = description
    fn.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict[str, Any]}
    return fn


_sources: dict[Path, GuardedSource] = {}
_sources_lock = threading.Lock()


def source_for(pack: Pack) -> GuardedSource:
    """The pack's guarded source, one per process (the server connection is reused)."""
    from replaybook.mock_env.remote import RemoteServer

    key = pack.path.resolve()
    with _sources_lock:
        if key not in _sources:
            spec = pack.knowledge
            assert spec is not None
            tools = load_tools(pack.tools_json) if pack.tools_json else []
            problems = spec.check_tools(tools)
            if problems:
                raise KnowledgeSpecError(f"pack '{pack.name}': " + "; ".join(problems))
            cache = ResponseCache(spec.cache.effective(pack.name), spec.server.describe())
            _sources[key] = GuardedSource(spec, tools, RemoteServer(spec.server), cache)
        return _sources[key]


def describe_packs(packs: list[Pack]) -> dict[str, dict[str, Any]]:
    """What a run records about the knowledge packs it could read: where each server is,
    whether its answers were live or pinned, and a digest of the pinned answers."""
    described: dict[str, dict[str, Any]] = {}
    for pack in packs:
        if pack.knowledge is None:
            continue
        cache = pack.knowledge.cache.effective(pack.name)
        described[pack.name] = {
            "server": pack.knowledge.server.describe(),
            "transport": pack.knowledge.server.transport,
            "cache": cache.mode,
            "cache_sha256": cache_digest(cache.dir) if cache.mode != "off" else None,
        }
    return described


def forget() -> None:
    """Close and forget every guarded source (packs.reset calls this)."""
    with _sources_lock:
        sources = list(_sources.values())
        _sources.clear()
    for source in sources:
        close = getattr(source.remote, "close", None)
        if close is not None:
            close()


def build_guarded_server(pack: Pack, world: WorldState, recorder: CallRecorder) -> MockServer:
    """The pack's tools, served from the team's own retrieval server behind the guard.
    Nothing connects until the agent calls a tool."""
    source = source_for(pack)
    description = pack.description or f"Past tickets from {pack.name}"
    server = MockServer(pack.name, f"{description}. {GUARD_NOTE}", world, recorder)
    for name in source.spec.tools:
        upstream = str(source.listed[name].get("description") or name)
        server.read(_make_tool(source, name, world, f"{upstream}\n\n{GUARD_NOTE}"))
    return server
