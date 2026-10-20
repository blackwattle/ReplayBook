"""The Postgres ticket adapter: tickets from a table a RAG pipeline or warehouse already has.

A YAML mapping names the connection, the tables and the columns; the rows go through the
same conversion as the csv/jsonl adapter. Status-change history is required: without it
there are no ask cases and nothing for the status_time effort source to measure, so the
import refuses unless told to proceed, and then warns in the terminal and the data card.

    dsn: ${POSTGRES_DSN}            # or set POSTGRES_DSN / REPLAYBOOK_POSTGRES_DSN
    source: warehouse
    project: OPS
    tickets:
      table: analytics.tickets      # schema.table or table
      columns: {id: ticket_key, summary: title, description: body, reporter: requester,
                created: opened_at, resolved: closed_at, status: state}
      where: "project = 'OPS'"      # optional SQL fragment
      limit: 50000                  # optional
    comments:
      table: analytics.ticket_comments
      columns: {ticket_id: ticket_key, author: author, created: posted_at, body: body}
    status_changes:                 # required unless allow_missing_history is set
      table: analytics.ticket_status_history
      columns: {ticket_id: ticket_key, at: changed_at, from_status: old_state, to_status: new_state}
    actions:                        # optional audit log of tool calls
      table: analytics.ticket_actions
      columns: {ticket_id: ticket_key, tool: tool, args: args, at: at}
    links:                          # optional links between tickets
      table: analytics.ticket_links
      columns: {ticket_id: ticket_key, target: linked_key, type: link_type}
    status_map: {"Pending customer": "Waiting on Requester"}
    engineers: [alice]
    effort: {source: status_time, working_statuses: [In Progress]}

Any section may name a `query` instead of a `table`: a SELECT the mapping's author writes,
for stores where the columns need shaping first (a constant reporter, a comment role from
a flag, a join) and the import has read access only, so no view can be created. The
columns then name the query's output columns, and `where` and `limit` apply around it:

    tickets:
      query: |
        SELECT t.key, t.title, t.body, 'unknown' AS reporter, t.opened_at, t.state
          FROM tickets t
      columns: {id: key, summary: title, description: body, reporter: reporter,
                created: opened_at, status: state}

Every connection is read-only (the session refuses writes), whichever form is used.

The driver (psycopg) is an optional extra: `uv sync --extra postgres`. Tests inject a fake
fetcher and never open a connection. Air-gapped mode cannot see libpq's own sockets, so
the database host is governed by the container network, not by REPLAYBOOK_ALLOWED_HOSTS.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from replaybook.adapters import tabular
from replaybook.adapters.tabular import Mapping, MappingError
from replaybook.schema.ticket import Ticket

Fetch = Callable[[str, list[str], str | None, int | None], list[dict[str, Any]]]
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
DSN_ENV = ("POSTGRES_DSN", "REPLAYBOOK_POSTGRES_DSN")


class PostgresMapping:
    def __init__(self, data: dict[str, Any], base: Path) -> None:
        self.base = base
        self.dsn = _resolve_dsn(data.get("dsn"))
        tickets = data.get("tickets")
        if not isinstance(tickets, dict) or "columns" not in tickets:
            raise MappingError("mapping needs tickets: {table: ..., columns: {...}}")
        self.tables: dict[str, str] = {"tickets": _source("tickets", tickets)}
        self.where = str(tickets.get("where") or "") or None
        self.limit = int(tickets["limit"]) if tickets.get("limit") else None
        self.allow_missing_history = bool(data.get("allow_missing_history", False))
        # Reuse the tabular mapping for columns, companions, status map, engineers, effort.
        shaped: dict[str, Any] = {
            k: v for k, v in data.items() if k not in ("dsn", "tickets", "allow_missing_history")
        }
        shaped["tickets"] = {
            "file": "query" if is_query(self.tables["tickets"]) else self.tables["tickets"],
            "columns": tickets["columns"],
            "list_separator": tickets.get("list_separator", ";"),
            "datetime_format": tickets.get("datetime_format"),
        }
        for section in tabular.COMPANIONS:
            spec = data.get(section)
            if spec:
                if "columns" not in spec:
                    raise MappingError(f"{section} needs table (or query) and columns")
                self.tables[section] = _source(section, spec)
                shaped[section] = {"file": section, "columns": spec["columns"]}
        self.tabular = Mapping(shaped, base)
        if "status_changes" not in self.tables and not self.allow_missing_history:
            raise MappingError(
                "no status_changes table is mapped. Replaybook needs status-change history "
                "to tell ask cases (engineers asked before acting) from the rest and to "
                "measure status_time effort. Map the history table, or set "
                "allow_missing_history: true (or --allow-missing-history) to import anyway "
                "with those features switched off."
            )

    @classmethod
    def load(cls, path: Path, allow_missing_history: bool = False) -> PostgresMapping:
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise MappingError(f"{path}: mapping must be a YAML object")
        if allow_missing_history:
            data["allow_missing_history"] = True
        return cls(data, path.parent)

    @property
    def has_history(self) -> bool:
        return "status_changes" in self.tables or "status_changes" in self.tabular.columns


def _resolve_dsn(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.startswith("${") and text.endswith("}"):
        text = os.environ.get(text[2:-1], "")
    if not text:
        for name in DSN_ENV:
            if os.environ.get(name):
                return os.environ[name]
        return None
    return text


QUERY_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def is_query(source: str) -> bool:
    """A section's source is a query (SELECT or WITH) rather than a table name."""
    return bool(QUERY_START.match(source))


def _source(section: str, spec: dict[str, Any]) -> str:
    """The table name, checked, or the query, which must be a single SELECT or WITH."""
    has_table, has_query = bool(spec.get("table")), bool(spec.get("query"))
    if has_table == has_query:
        raise MappingError(f"{section} needs exactly one of table or query")
    if has_table:
        return _check_table(str(spec["table"]))
    query = str(spec["query"]).strip().rstrip(";").strip()
    if not is_query(query) or ";" in query:
        raise MappingError(f"{section}.query must be one SELECT (or WITH ... SELECT) statement")
    return query


def _check_table(name: str) -> str:
    parts = name.split(".")
    if len(parts) > 2 or not all(IDENTIFIER.match(p) for p in parts):
        raise MappingError(f"table name '{name}' must be identifier or schema.identifier")
    return name


def quote(identifier: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in identifier.split("."))


def build_query(table: str, columns: list[str], where: str | None, limit: int | None) -> str:
    cols = ", ".join(quote(c) for c in columns) if columns else "*"
    source = f"({table}) AS mapped" if is_query(table) else quote(table)
    sql = f"SELECT {cols} FROM {source}"
    if where:
        sql += f" WHERE {where}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql


def psycopg_fetch(dsn: str | None) -> Fetch:
    """A fetcher over a real connection. Imported lazily: the driver is optional."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise MappingError(
            "the postgres extra is not installed: run `uv sync --extra postgres`"
        ) from exc
    if not dsn:
        raise MappingError("no connection string: set dsn in the mapping or POSTGRES_DSN")

    def fetch(
        table: str, columns: list[str], where: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            conn.read_only = True  # the import only reads; the session refuses any write
            with conn.cursor() as cur:
                cur.execute(build_query(table, columns, where, limit))  # type: ignore[arg-type]
                return [dict(row) for row in cur.fetchall()]

    return fetch


def read_tickets(mapping: PostgresMapping, fetch: Fetch) -> list[Ticket]:
    tab = mapping.tabular
    rows = fetch(
        mapping.tables["tickets"], list(tab.columns.values()), mapping.where, mapping.limit
    )
    companions: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for section in tabular.COMPANIONS:
        if section not in mapping.tables:
            continue
        _path, columns = tab.companions[section]
        by_ticket: dict[str, list[dict[str, Any]]] = {}
        for row in fetch(mapping.tables[section], list(columns.values()), None, None):
            ticket_id = str(row.get(columns["ticket_id"], "")).strip()
            if ticket_id:
                by_ticket.setdefault(ticket_id, []).append(
                    {f: row.get(c) for f, c in columns.items() if f != "ticket_id"}
                )
        companions[section] = by_ticket
    return tabular.convert_rows(
        tab,
        rows,
        companions.get("comments", {}),
        companions.get("status_changes", {}),
        companions.get("actions", {}),
        companions.get("links", {}),
    )


def import_tickets(
    mapping_path: Path,
    out_dir: Path,
    fetch: Fetch | None = None,
    allow_missing_history: bool = False,
) -> dict[str, Any]:
    mapping = PostgresMapping.load(mapping_path, allow_missing_history)
    fetcher = fetch if fetch is not None else psycopg_fetch(mapping.dsn)
    tickets = read_tickets(mapping, fetcher)
    stats = tabular.write_dataset(tickets, mapping.tabular, out_dir)
    stats["history_mapped"] = mapping.has_history
    stats["tables"] = dict(mapping.tables)
    return stats
