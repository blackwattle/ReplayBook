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
    status_map: {"Pending customer": "Waiting on Requester"}
    engineers: [alice]
    effort: {source: status_time, working_statuses: [In Progress]}

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
        if not isinstance(tickets, dict) or "table" not in tickets or "columns" not in tickets:
            raise MappingError("mapping needs tickets: {table: ..., columns: {...}}")
        self.tables: dict[str, str] = {"tickets": _check_table(str(tickets["table"]))}
        self.where = str(tickets.get("where") or "") or None
        self.limit = int(tickets["limit"]) if tickets.get("limit") else None
        self.allow_missing_history = bool(data.get("allow_missing_history", False))
        # Reuse the tabular mapping for columns, companions, status map, engineers, effort.
        shaped: dict[str, Any] = {
            k: v for k, v in data.items() if k not in ("dsn", "tickets", "allow_missing_history")
        }
        shaped["tickets"] = {
            "file": str(tickets["table"]),
            "columns": tickets["columns"],
            "list_separator": tickets.get("list_separator", ";"),
            "datetime_format": tickets.get("datetime_format"),
        }
        for section in ("comments", "status_changes", "actions"):
            spec = data.get(section)
            if spec:
                if "table" not in spec or "columns" not in spec:
                    raise MappingError(f"{section} needs table and columns")
                self.tables[section] = _check_table(str(spec["table"]))
                shaped[section] = {"file": str(spec["table"]), "columns": spec["columns"]}
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


def _check_table(name: str) -> str:
    parts = name.split(".")
    if len(parts) > 2 or not all(IDENTIFIER.match(p) for p in parts):
        raise MappingError(f"table name '{name}' must be identifier or schema.identifier")
    return name


def quote(identifier: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in identifier.split("."))


def build_query(table: str, columns: list[str], where: str | None, limit: int | None) -> str:
    cols = ", ".join(quote(c) for c in columns) if columns else "*"
    sql = f"SELECT {cols} FROM {quote(table)}"
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
    for section in ("comments", "status_changes", "actions"):
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
