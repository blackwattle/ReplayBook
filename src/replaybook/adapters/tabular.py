"""The csv/jsonl ticket adapter: the universal fallback for any ticket system.

Every ticket system can export a table. A YAML mapping names the file(s) and which column
holds which canonical field; this module turns the rows into canonical tickets, redacts
them, applies the configured effort source and writes a dataset directory (tickets.jsonl,
platform.yaml, DATASET.md). docs/ticket-sources.md documents the mapping with a worked
example; `replaybook tickets import --mapping mapping.yaml --out data/x` runs it.

Shape of a mapping (every section but `tickets` is optional):

    source: servicedesk              # recorded on every ticket
    project: OPS
    tickets:
      file: tickets.csv              # .csv, .jsonl or .json, relative to the mapping file
      columns:                       # canonical field -> column name
        id: Issue key
        summary: Summary
        description: Description
        reporter: Reporter
        created: Created
        status: Status
        resolved: Resolved           # optional from here on
        resolution: Resolution
        assignee: Assignee
        priority: Priority
        labels: Labels
        components: Components
        time_spent_hours: Time spent (h)     # or time_spent_seconds / time_spent_minutes
        comments: Comments           # a JSON list in the cell, instead of a companion file
        status_changes: History
        actions: Actions
        inventory: Inventory         # a JSON object: the CMDB snapshot the packs read
      list_separator: ";"
      datetime_format: null          # ISO 8601 when null; otherwise a strptime format
    comments:                        # companion table, one row per comment
      file: comments.csv
      columns: {ticket_id: Issue key, author: Author, created: Created, body: Body, role: Role}
    status_changes:                  # companion table, one row per transition
      file: history.csv
      columns: {ticket_id: Issue key, at: Changed, from_status: From, to_status: To}
    actions:                         # companion table or JSONL: an audit log of tool calls
      file: actions.jsonl
      columns: {ticket_id: ticket_id, tool: tool, args: args, at: at}
    status_map: {"Pending customer": "Waiting on Requester", "Done": "Resolved"}
    engineers: [alice, bob]          # authors treated as engineers (plus the assignee)
    effort:
      source: logged                 # logged | status_time | created_to_resolved
      working_statuses: [In Progress, Reopened]
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from replaybook.adapters import effort as effort_mod
from replaybook.adapters.redact import redact_ticket
from replaybook.schema.ticket import (
    Comment,
    CommentRole,
    EngineerAction,
    StatusChange,
    Ticket,
)

REQUIRED = ("id", "summary", "description", "reporter", "created", "status")
OPTIONAL = (
    "resolved",
    "resolution",
    "assignee",
    "priority",
    "labels",
    "components",
    "time_spent_hours",
    "time_spent_seconds",
    "time_spent_minutes",
    "comments",
    "status_changes",
    "actions",
    "inventory",
)
CANONICAL_STATUSES = ("Open", "In Progress", "Waiting on Requester", "Resolved", "Closed")


class MappingError(Exception):
    """The mapping or the data does not fit."""


# --- reading rows ------------------------------------------------------------------------


def read_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise MappingError(f"{path}:{number}: expected a JSON object per line")
                rows.append(row)
        return rows
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("tickets") or data.get("issues") or data.get("rows") or []
        if not isinstance(data, list):
            raise MappingError(f"{path}: expected a JSON list")
        return [dict(row) for row in data]
    raise MappingError(f"{path}: unsupported file type '{suffix}' (csv, jsonl or json)")


def parse_datetime(value: Any, fmt: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, int | float):
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    else:
        text = str(value).strip()
        try:
            parsed = (
                datetime.strptime(text, fmt)
                if fmt
                else datetime.fromisoformat(text.replace("Z", "+00:00"))
            )
        except ValueError as exc:
            raise MappingError(f"cannot parse datetime '{text}'") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_list(value: Any, separator: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value).strip()
    if text.startswith("["):
        try:
            return [str(v) for v in json.loads(text)]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in text.split(separator) if part.strip()]


def parse_embedded(value: Any) -> list[dict[str, Any]]:
    """A JSON list in a cell (CSV) or a real list (JSONL)."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [dict(v) for v in value]
    try:
        data = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise MappingError(f"expected a JSON list in the cell, got {str(value)[:40]!r}") from exc
    return [dict(v) for v in data] if isinstance(data, list) else []


def parse_object(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        data = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise MappingError(f"expected a JSON object in the cell, got {str(value)[:40]!r}") from exc
    return dict(data) if isinstance(data, dict) else {}


# --- the mapping -------------------------------------------------------------------------


class Mapping:
    def __init__(self, data: dict[str, Any], base: Path) -> None:
        self.base = base
        self.source = str(data.get("source") or "tabular")
        self.project = str(data.get("project") or "TICKETS")
        tickets = data.get("tickets")
        if not isinstance(tickets, dict) or "file" not in tickets or "columns" not in tickets:
            raise MappingError("mapping needs tickets: {file: ..., columns: {...}}")
        self.tickets_file = base / str(tickets["file"])
        self.columns: dict[str, str] = {str(k): str(v) for k, v in tickets["columns"].items()}
        missing = [field for field in REQUIRED if field not in self.columns]
        if missing:
            raise MappingError(f"tickets.columns is missing {', '.join(missing)}")
        unknown = [f for f in self.columns if f not in REQUIRED and f not in OPTIONAL]
        if unknown:
            raise MappingError(
                f"tickets.columns has unknown fields {', '.join(unknown)}; "
                f"known: {', '.join(REQUIRED + OPTIONAL)}"
            )
        self.list_separator = str(tickets.get("list_separator") or ";")
        self.datetime_format = tickets.get("datetime_format") or None
        self.companions: dict[str, tuple[Path, dict[str, str]]] = {}
        for section in ("comments", "status_changes", "actions"):
            spec = data.get(section)
            if spec:
                if "file" not in spec or "columns" not in spec:
                    raise MappingError(f"{section} needs file and columns")
                self.companions[section] = (
                    base / str(spec["file"]),
                    {str(k): str(v) for k, v in spec["columns"].items()},
                )
        self.status_map = {str(k): str(v) for k, v in (data.get("status_map") or {}).items()}
        self.engineers = {str(e) for e in (data.get("engineers") or [])}
        effort = data.get("effort") or {}
        self.effort_source = effort_mod.check_source(str(effort.get("source") or "logged"))
        self.working_statuses = tuple(
            str(s) for s in (effort.get("working_statuses") or effort_mod.WORKING_STATUSES)
        )

    @classmethod
    def load(cls, path: Path) -> Mapping:
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise MappingError(f"{path}: mapping must be a YAML object")
        return cls(data, path.parent)


# --- conversion --------------------------------------------------------------------------


def _companion_rows(mapping: Mapping, section: str) -> dict[str, list[dict[str, Any]]]:
    if section not in mapping.companions:
        return {}
    path, columns = mapping.companions[section]
    if "ticket_id" not in columns:
        raise MappingError(f"{section}.columns needs ticket_id")
    by_ticket: dict[str, list[dict[str, Any]]] = {}
    for row in read_rows(path):
        ticket_id = str(row.get(columns["ticket_id"], "")).strip()
        if not ticket_id:
            continue
        by_ticket.setdefault(ticket_id, []).append(
            {field: row.get(column) for field, column in columns.items() if field != "ticket_id"}
        )
    return by_ticket


def _status(mapping: Mapping, value: Any) -> str:
    text = str(value or "").strip()
    return mapping.status_map.get(text, text)


def _role(
    author: str, reporter: str, assignee: str | None, mapping: Mapping, given: Any
) -> CommentRole:
    if given in ("requester", "engineer", "bot", "other"):
        return given  # type: ignore[return-value]
    if author and author == reporter:
        return "requester"
    if author and (author in mapping.engineers or author == assignee):
        return "engineer"
    return "other"


def row_to_ticket(
    row: dict[str, Any],
    mapping: Mapping,
    comments: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> Ticket:
    col = mapping.columns
    fmt = mapping.datetime_format

    def get(field: str) -> Any:
        return row.get(col[field]) if field in col else None

    ticket_id = str(get("id") or "").strip()
    if not ticket_id:
        raise MappingError("a row has no id")
    created = parse_datetime(get("created"), fmt)
    if created is None:
        raise MappingError(f"{ticket_id}: created is empty")
    reporter = str(get("reporter") or "").strip() or "unknown"
    assignee = str(get("assignee") or "").strip() or None
    hours: float | None = None
    for field, factor in (
        ("time_spent_hours", 1.0),
        ("time_spent_minutes", 60.0),
        ("time_spent_seconds", 3600.0),
    ):
        raw = get(field)
        if raw not in (None, ""):
            hours = round(float(raw) / factor, 2)
            break
    if "comments" in col:
        comments = comments + parse_embedded(get("comments"))
    if "status_changes" in col:
        changes = changes + parse_embedded(get("status_changes"))
    if "actions" in col:
        actions = actions + parse_embedded(get("actions"))

    built_comments = [
        Comment(
            author=str(c.get("author") or "unknown"),
            role=_role(str(c.get("author") or ""), reporter, assignee, mapping, c.get("role")),
            created=parse_datetime(c.get("created"), fmt) or created,
            body=str(c.get("body") or ""),
        )
        for c in comments
    ]
    built_changes = [
        StatusChange(
            at=parse_datetime(c.get("at"), fmt) or created,
            from_status=_status(mapping, c.get("from_status")) or None,
            to_status=_status(mapping, c.get("to_status")),
        )
        for c in changes
        if c.get("to_status")
    ]
    built_actions = []
    for a in actions:
        args = a.get("args") or {}
        if isinstance(args, str):
            args = json.loads(args) if args.strip() else {}
        built_actions.append(
            EngineerAction(
                tool=str(a.get("tool")),
                args=dict(args),
                at=parse_datetime(a.get("at"), fmt) or created,
                source=str(a.get("source") or "audit_log"),
            )
        )
    resolved = parse_datetime(get("resolved"), fmt)
    status = _status(mapping, get("status")) or "Open"
    synthesised_history = not built_changes
    if synthesised_history:
        built_changes.append(StatusChange(at=created, from_status=None, to_status="Open"))
        if resolved is not None and status != "Open":
            built_changes.append(StatusChange(at=resolved, from_status="Open", to_status=status))
    ticket = Ticket(
        id=ticket_id,
        source=mapping.source,
        project=mapping.project,
        summary=str(get("summary") or "").strip(),
        description=str(get("description") or ""),
        reporter=reporter,
        assignee=assignee,
        created=created,
        resolved=resolved,
        status=status,
        resolution=(str(get("resolution")).strip() or None)
        if get("resolution") not in (None, "")
        else None,
        priority=str(get("priority") or "Medium"),
        labels=parse_list(get("labels"), mapping.list_separator),
        components=parse_list(get("components"), mapping.list_separator),
        comments=sorted(built_comments, key=lambda c: c.created),
        status_changes=sorted(built_changes, key=lambda c: c.at),
        actions=sorted(built_actions, key=lambda a: a.at),
        time_spent_hours=hours,
        inventory=parse_object(get("inventory")) if "inventory" in col else {},
    )
    if synthesised_history and mapping.effort_source == "status_time":
        # A two-step Open/final history is a placeholder, not a measurement.
        return ticket.model_copy(update={"time_spent_hours": None, "effort_source": None})
    return effort_mod.apply_effort(ticket, mapping.effort_source, mapping.working_statuses)


def convert_rows(
    mapping: Mapping,
    rows: list[dict[str, Any]],
    comments: dict[str, list[dict[str, Any]]],
    changes: dict[str, list[dict[str, Any]]],
    actions: dict[str, list[dict[str, Any]]],
) -> list[Ticket]:
    """Rows already read (from files or a database) to redacted, sorted canonical tickets."""
    tickets: list[Ticket] = []
    seen: set[str] = set()
    id_column = mapping.columns["id"]
    for row in rows:
        ticket_id = str(row.get(id_column, "")).strip()
        ticket = row_to_ticket(
            row,
            mapping,
            comments.get(ticket_id, []),
            changes.get(ticket_id, []),
            actions.get(ticket_id, []),
        )
        if ticket.id in seen:
            raise MappingError(f"duplicate ticket id {ticket.id}")
        seen.add(ticket.id)
        tickets.append(redact_ticket(ticket))
    tickets.sort(key=lambda t: (t.created, t.id))
    return tickets


def convert(mapping: Mapping) -> list[Ticket]:
    return convert_rows(
        mapping,
        read_rows(mapping.tickets_file),
        _companion_rows(mapping, "comments"),
        _companion_rows(mapping, "status_changes"),
        _companion_rows(mapping, "actions"),
    )


# --- the dataset directory ---------------------------------------------------------------


def write_dataset(tickets: list[Ticket], mapping: Mapping, out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "tickets.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for ticket in tickets:
            handle.write(ticket.model_dump_json())
            handle.write("\n")
    platform = out_dir / "platform.yaml"
    if not platform.exists():
        platform.write_text(
            "# Procedure changes for this history, if any (request_type, at, from_version,\n"
            "# to_version), and any facts the packs read. Nothing is known automatically.\n"
            "procedure_changes: []\n",
            encoding="utf-8",
        )
    stats = summarise(tickets, mapping)
    (out_dir / "DATASET.md").write_text(data_card(stats, mapping), encoding="utf-8")
    return stats


def summarise(tickets: list[Ticket], mapping: Mapping) -> dict[str, Any]:
    statuses = Counter(t.status for t in tickets)
    unknown = sorted(s for s in statuses if s not in CANONICAL_STATUSES)
    return {
        "source": mapping.source,
        "project": mapping.project,
        "tickets": len(tickets),
        "comments": sum(len(t.comments) for t in tickets),
        "status_changes": sum(len(t.status_changes) for t in tickets),
        "with_status_history": sum(len(t.status_changes) > 2 for t in tickets),
        "waited_on_requester": sum(
            any(c.to_status == "Waiting on Requester" for c in t.status_changes) for t in tickets
        ),
        "with_actions": sum(bool(t.actions) for t in tickets),
        "with_effort": sum(t.time_spent_hours is not None for t in tickets),
        "effort_source": mapping.effort_source,
        "working_statuses": list(mapping.working_statuses),
        "by_status": dict(statuses.most_common()),
        "unknown_statuses": unknown,
        "oldest": min((t.created for t in tickets), default=None),
        "newest": max((t.created for t in tickets), default=None),
    }


def data_card(stats: dict[str, Any], mapping: Mapping) -> str:
    oldest = str(stats["oldest"])[:10] if stats["oldest"] else "-"
    newest = str(stats["newest"])[:10] if stats["newest"] else "-"
    status_rows = "\n".join(f"| {name} | {count} |" for name, count in stats["by_status"].items())
    unknown = (
        "- Statuses not in the canonical set (Open, In Progress, Waiting on Requester, Resolved, "
        f"Closed): {', '.join(stats['unknown_statuses'])}. Add them to `status_map` in the "
        "mapping if they mean one of those; a waiting-on-the-requester status is what makes an "
        '"ask" case.\n'
        if stats["unknown_statuses"]
        else "- Every status is canonical.\n"
    )
    history = (
        "- Status-change history is present for "
        f"{stats['with_status_history']} of {stats['tickets']} tickets.\n"
        if stats["with_status_history"]
        else "- **No status-change history.** Ask cases (engineers asked the requester before "
        "acting) cannot be detected and the status_time effort source has nothing to measure; "
        "map a history table if the system has one.\n"
    )
    return f"""# Dataset: {stats["source"]}, project {stats["project"]}

Imported from `{mapping.tickets_file.name}` by `replaybook tickets import` with the mapping in
`{mapping.base}`. Redacted at ingest (emails, public IP addresses, secret-looking strings).

| Measure | Value |
|---|---|
| Tickets | {stats["tickets"]} |
| Date range | {oldest} to {newest} |
| Comments | {stats["comments"]} |
| Status changes | {stats["status_changes"]} |
| Tickets that waited on the requester | {stats["waited_on_requester"]} |
| Tickets with recorded engineer actions | {stats["with_actions"]} |
| Tickets with an effort figure | {stats["with_effort"]} |
| Effort source | {effort_mod.describe(stats["effort_source"], stats["working_statuses"])} |

## Status

| Status | Tickets |
|---|---|
{status_rows}

## Notes

{unknown}{history}- Tickets without recorded actions get their expectations from the text
  (`replaybook build-testset --extract rules` or `model`), labelled unverified until a
  person confirms them.
"""


def import_tickets(mapping_path: Path, out_dir: Path) -> dict[str, Any]:
    mapping = Mapping.load(mapping_path)
    tickets = convert(mapping)
    return write_dataset(tickets, mapping, out_dir)
