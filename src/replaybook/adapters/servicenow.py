"""ServiceNow as a ticket source: the Table API, paginated, with journal and audit history.

Credentials come from .env (SERVICENOW_INSTANCE, SERVICENOW_USER, SERVICENOW_PASSWORD) and
are only sent as the Basic auth header to that instance. The air-gap guard applies: in
air-gapped mode the instance has to be in REPLAYBOOK_ALLOWED_HOSTS. Nothing here writes to
ServiceNow.

Endpoints, as documented for the Table API (all GET, JSON):
- /api/now/table/{table}?sysparm_query=...&sysparm_fields=...&sysparm_limit=N
  &sysparm_offset=M&sysparm_display_value=all&sysparm_exclude_reference_link=true
  -> {"result": [ {field: {"display_value": "...", "value": "..."}, ...} ]}
  Paging: offsets until a page comes back shorter than the limit.
- /api/now/table/sys_journal_field?sysparm_query=name={table}^element_idIN<ids>
  -> comments ("comments") and work notes ("work_notes") per record
- /api/now/table/sys_audit?sysparm_query=tablename={table}^documentidIN<ids>
  ^fieldnameINstate,hold_reason -> state transitions with numeric codes

Tables: incident and sc_req_item (requested items). States are mapped onto the canonical
names; an incident On Hold with hold reason Awaiting Caller is "Waiting on Requester", the
other hold reasons (vendor, change, problem) are not the requester's wait. User references
are reduced to sys_ids in the raw copy; user names are used in memory only to tell the
caller's comments from the fulfiller's.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dotenv import load_dotenv

from replaybook.adapters.effort import apply_effort_all, describe
from replaybook.adapters.redact import redact_ticket
from replaybook.schema.ticket import Comment, CommentRole, StatusChange, Ticket

SOURCE = "servicenow"
PAGE_SIZE = 200
ID_BATCH = 50
Fetch = Callable[[str, dict[str, str]], dict[str, Any]]

INCIDENT_FIELDS = (
    "sys_id,number,short_description,description,opened_at,resolved_at,closed_at,"
    "sys_created_on,state,hold_reason,close_code,close_notes,priority,category,subcategory,"
    "assignment_group,time_worked,opened_by,caller_id,assigned_to,"
    "opened_by.user_name,caller_id.user_name,assigned_to.user_name"
)
REQ_ITEM_FIELDS = (
    "sys_id,number,short_description,description,opened_at,closed_at,sys_created_on,state,"
    "stage,priority,cat_item,assignment_group,opened_by,assigned_to,request.requested_for,"
    "opened_by.user_name,assigned_to.user_name,request.requested_for.user_name"
)
TABLE_FIELDS = {"incident": INCIDENT_FIELDS, "sc_req_item": REQ_ITEM_FIELDS}
REFERENCE_FIELDS = ("opened_by", "caller_id", "assigned_to", "request.requested_for")

# State codes -> ServiceNow display names, per table (the audit log carries the codes).
INCIDENT_STATES = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Canceled",
}
REQ_ITEM_STATES = {
    "-5": "Pending",
    "1": "Open",
    "2": "Work in Progress",
    "3": "Closed Complete",
    "4": "Closed Incomplete",
    "7": "Closed Skipped",
}
STATE_CODES = {"incident": INCIDENT_STATES, "sc_req_item": REQ_ITEM_STATES}
HOLD_AWAITING_CALLER = "1"
CANONICAL = {
    "new": "Open",
    "open": "Open",
    "pending": "Open",
    "in progress": "In Progress",
    "work in progress": "In Progress",
    "on hold": "In Progress",  # refined by the hold reason
    "resolved": "Resolved",
    "closed": "Closed",
    "closed complete": "Resolved",
    "closed incomplete": "Closed",
    "closed skipped": "Closed",
    "canceled": "Closed",
    "cancelled": "Closed",
}


class ServiceNowError(RuntimeError):
    """Configuration or HTTP problem talking to ServiceNow."""


@dataclass(frozen=True)
class ServiceNowConfig:
    instance: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> ServiceNowConfig:
        load_dotenv(override=False)
        values = {
            name: os.environ.get(name, "").strip()
            for name in ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD")
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ServiceNowError(
                f"set {', '.join(missing)} in .env (see docs/ticket-sources.md for the setup steps)"
            )
        instance = values["SERVICENOW_INSTANCE"].rstrip("/")
        if not instance.startswith("http"):
            instance = "https://" + instance
        return cls(instance, values["SERVICENOW_USER"], values["SERVICENOW_PASSWORD"])


# --- field helpers -------------------------------------------------------------------------


def value(record: dict[str, Any], field: str) -> str:
    """The raw value of a field returned with sysparm_display_value=all (or plain)."""
    item = record.get(field)
    if isinstance(item, dict):
        return str(item.get("value") or "")
    return "" if item is None else str(item)


def display(record: dict[str, Any], field: str) -> str:
    item = record.get(field)
    if isinstance(item, dict):
        return str(item.get("display_value") or item.get("value") or "")
    return "" if item is None else str(item)


def parse_time(text: str) -> datetime | None:
    """ServiceNow returns 'YYYY-MM-DD HH:MM:SS' in the instance time zone for display values
    and in UTC for raw values; we read raw values and take them as UTC."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def duration_hours(text: str) -> float | None:
    """time_worked raw value is a datetime offset from 1970-01-01 ('1970-01-01 00:45:00')."""
    parsed = parse_time(text)
    if parsed is None:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    hours = (parsed - epoch).total_seconds() / 3600
    return round(hours, 2) if hours > 0 else None


def canonical_state(table: str, state_display: str, hold_reason: str = "") -> str:
    name = state_display.strip().lower()
    if name == "on hold":
        return "Waiting on Requester" if hold_reason == HOLD_AWAITING_CALLER else "In Progress"
    return CANONICAL.get(name, state_display.strip() or "Open")


def canonical_resolution(table: str, record: dict[str, Any]) -> str | None:
    state = display(record, "state").strip().lower()
    if table == "incident":
        code = display(record, "close_code").strip()
        if state == "canceled" or state == "cancelled":
            return "Canceled"
        if not code:
            return None
        lowered = code.lower()
        if "duplicate" in lowered:
            return "Duplicate"
        if lowered.startswith("solved") or "resolved by caller" in lowered:
            return "Done"
        if lowered.startswith("not solved"):
            return "Not Solved"
        return code
    if state == "closed complete":
        return "Done"
    if state == "closed incomplete":
        return "Incomplete"
    if state == "closed skipped":
        return "Skipped"
    return None


def strip_users(record: dict[str, Any]) -> dict[str, Any]:
    """Reference fields keep their sys_id only; dot-walked user names are dropped."""
    cleaned: dict[str, Any] = {}
    for key, item in record.items():
        if key.endswith(".user_name"):
            continue
        if key in REFERENCE_FIELDS and isinstance(item, dict):
            cleaned[key] = {"value": item.get("value") or ""}
        else:
            cleaned[key] = item
    return cleaned


# --- conversion ----------------------------------------------------------------------------


def _role(
    author: str, caller_name: str, assignee_name: str, element: str, engineers: set[str]
) -> CommentRole:
    if element == "work_notes":
        return "engineer"
    if author and author == caller_name:
        return "requester"
    if author and (author == assignee_name or author in engineers):
        return "engineer"
    if author.lower() in ("system", "guest", "admin.integration"):
        return "bot"
    return "other"


def record_to_ticket(
    table: str,
    record: dict[str, Any],
    journal: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    engineers: set[str] | None = None,
) -> Ticket:
    engineers = engineers or set()
    codes = STATE_CODES.get(table, {})
    created = parse_time(value(record, "opened_at")) or parse_time(value(record, "sys_created_on"))
    if created is None:
        raise ServiceNowError(f"{display(record, 'number')}: no opened_at")
    requester_field = "caller_id" if table == "incident" else "request.requested_for"
    caller_name = value(record, f"{requester_field}.user_name") or value(
        record, "opened_by.user_name"
    )
    assignee_name = value(record, "assigned_to.user_name")
    reporter = value(record, requester_field) or value(record, "opened_by") or "unknown"
    assignee = value(record, "assigned_to") or None

    comments: list[Comment] = []
    for entry in sorted(journal, key=lambda e: value(e, "sys_created_on")):
        at = parse_time(value(entry, "sys_created_on")) or created
        author = value(entry, "sys_created_by")
        element = value(entry, "element")
        comments.append(
            Comment(
                author=author or "unknown",
                role=_role(author, caller_name, assignee_name, element, engineers),
                created=at,
                body=value(entry, "value"),
            )
        )

    # State transitions from the audit log; hold reason audits refine "On Hold".
    hold_at: list[tuple[datetime, str]] = []
    state_audits: list[tuple[datetime, str, str]] = []
    for audit in audits:
        at = parse_time(value(audit, "sys_created_on"))
        if at is None:
            continue
        field = value(audit, "fieldname")
        if field == "hold_reason":
            hold_at.append((at, value(audit, "newvalue")))
        elif field == "state":
            state_audits.append((at, value(audit, "oldvalue"), value(audit, "newvalue")))
    hold_at.sort()
    changes: list[StatusChange] = [StatusChange(at=created, from_status=None, to_status="Open")]
    previous = "Open"
    for at, old_code, new_code in sorted(state_audits):
        hold = next(
            (reason for t, reason in reversed(hold_at) if t <= at), value(record, "hold_reason")
        )
        new_status = canonical_state(table, codes.get(new_code, new_code), hold)
        old_status = (
            canonical_state(table, codes.get(old_code, old_code), hold) if old_code else previous
        )
        if new_status == previous and old_status == previous:
            continue
        changes.append(StatusChange(at=at, from_status=previous, to_status=new_status))
        previous = new_status
    final_status = canonical_state(table, display(record, "state"), value(record, "hold_reason"))
    resolved = parse_time(value(record, "resolved_at")) or parse_time(value(record, "closed_at"))
    if previous != final_status:
        changes.append(
            StatusChange(
                at=resolved or changes[-1].at, from_status=previous, to_status=final_status
            )
        )
    labels = [
        x
        for x in (
            display(record, "category"),
            display(record, "subcategory"),
            display(record, "cat_item"),
        )
        if x
    ]
    priority = display(record, "priority")
    if " - " in priority:
        priority = priority.split(" - ", 1)[1]
    group = display(record, "assignment_group")
    return Ticket(
        id=display(record, "number") or value(record, "sys_id"),
        source=SOURCE,
        project=table,
        summary=display(record, "short_description"),
        description=display(record, "description"),
        reporter=reporter,
        assignee=assignee,
        created=created,
        resolved=resolved if final_status in ("Resolved", "Closed") else None,
        status=final_status,
        resolution=canonical_resolution(table, record)
        if final_status in ("Resolved", "Closed")
        else None,
        priority=priority or "Medium",
        labels=labels,
        components=[group] if group else [],
        comments=comments,
        status_changes=changes,
        time_spent_hours=duration_hours(value(record, "time_worked")),
    )


# --- client --------------------------------------------------------------------------------


class ServiceNow:
    def __init__(self, config: ServiceNowConfig, fetch: Fetch | None = None) -> None:
        self.config = config
        self.fetch = fetch or self._http_get

    def _headers(self) -> dict[str, str]:
        raw = f"{self.config.user}:{self.config.password}".encode()
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Accept": "application/json",
        }

    def _http_get(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        shown = url.split("?")[0]
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ServiceNowError(f"HTTP {exc.code} from {shown}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            hint = (
                " (air-gapped mode: add the instance to REPLAYBOOK_ALLOWED_HOSTS)"
                if "air-gapped" in str(reason)
                else ""
            )
            raise ServiceNowError(f"cannot reach {shown}: {reason}{hint}") from exc

    def _get(self, table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{self.config.instance}/api/now/table/{table}?{urlencode(params)}"
        page = self.fetch(url, self._headers())
        result = page.get("result", []) if isinstance(page, dict) else []
        return list(result or [])

    def records(
        self, table: str, query: str = "", fields: str = "", page_size: int = PAGE_SIZE
    ) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            params: dict[str, Any] = {
                "sysparm_query": query or "ORDERBYsys_created_on",
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
                "sysparm_limit": page_size,
                "sysparm_offset": offset,
            }
            if fields:
                params["sysparm_fields"] = fields
            batch = self._get(table, params)
            yield from batch
            if len(batch) < page_size:
                return
            offset += len(batch)

    def _by_record(
        self, table: str, id_field: str, sys_ids: list[str], extra: str, fields: str
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for start in range(0, len(sys_ids), ID_BATCH):
            ids = ",".join(sys_ids[start : start + ID_BATCH])
            query = f"{extra}^{id_field}IN{ids}^ORDERBYsys_created_on"
            for row in self.records(table, query, fields):
                grouped.setdefault(value(row, id_field), []).append(row)
        return grouped

    def journal(self, table: str, sys_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return self._by_record(
            "sys_journal_field",
            "element_id",
            sys_ids,
            f"name={table}^elementINcomments,work_notes",
            "element_id,element,value,sys_created_on,sys_created_by",
        )

    def audits(self, table: str, sys_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return self._by_record(
            "sys_audit",
            "documentid",
            sys_ids,
            f"tablename={table}^fieldnameINstate,hold_reason",
            "documentid,fieldname,oldvalue,newvalue,sys_created_on,user",
        )

    # --- export ---------------------------------------------------------------------------

    def export_table(
        self,
        table: str,
        out_dir: Path,
        query: str = "",
        effort: str = "status_time",
        engineers: set[str] | None = None,
    ) -> dict[str, Any]:
        if table not in TABLE_FIELDS:
            raise ServiceNowError(f"table must be one of {', '.join(TABLE_FIELDS)}")
        out_dir = Path(out_dir)
        raw_path = out_dir / "raw" / f"{table}.jsonl.gz"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        records = list(self.records(table, query, TABLE_FIELDS[table]))
        sys_ids = [value(r, "sys_id") for r in records if value(r, "sys_id")]
        journal = self.journal(table, sys_ids)
        audits = self.audits(table, sys_ids)
        tickets: list[Ticket] = []
        with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
            for record in records:
                sys_id = value(record, "sys_id")
                handle.write(
                    json.dumps(
                        {
                            "record": strip_users(record),
                            "journal": journal.get(sys_id, []),
                            "audit": audits.get(sys_id, []),
                        },
                        default=str,
                    )
                )
                handle.write("\n")
                tickets.append(
                    redact_ticket(
                        record_to_ticket(
                            table,
                            record,
                            journal.get(sys_id, []),
                            audits.get(sys_id, []),
                            engineers,
                        )
                    )
                )
        tickets = apply_effort_all(sorted(tickets, key=lambda t: (t.created, t.id)), effort)
        with (out_dir / "tickets.jsonl").open("w", encoding="utf-8") as handle:
            for ticket in tickets:
                handle.write(ticket.model_dump_json())
                handle.write("\n")
        (out_dir / "platform.yaml").write_text(
            "# Procedure changes for this queue, if any (request_type, at, from_version,\n"
            "# to_version). Nothing is known automatically.\n"
            "procedure_changes: []\n",
            encoding="utf-8",
        )
        stats = summarise(tickets, table, self.config.instance, effort)
        (out_dir / "DATASET.md").write_text(data_card(stats), encoding="utf-8")
        stats["raw_documents"] = len(records)
        return stats


def summarise(tickets: list[Ticket], table: str, instance: str, effort: str) -> dict[str, Any]:
    statuses = Counter(t.status for t in tickets)
    return {
        "table": table,
        "instance": instance,
        "tickets": len(tickets),
        "comments": sum(len(t.comments) for t in tickets),
        "status_changes": sum(len(t.status_changes) for t in tickets),
        "with_status_history": sum(len(t.status_changes) > 2 for t in tickets),
        "waited_on_requester": sum(
            any(c.to_status == "Waiting on Requester" for c in t.status_changes) for t in tickets
        ),
        "with_effort": sum(t.time_spent_hours is not None for t in tickets),
        "effort_source": effort,
        "by_status": dict(statuses.most_common()),
        "by_resolution": dict(Counter(t.resolution or "(none)" for t in tickets).most_common()),
        "oldest": min((t.created for t in tickets), default=None),
        "newest": max((t.created for t in tickets), default=None),
    }


def data_card(stats: dict[str, Any]) -> str:
    oldest = str(stats["oldest"])[:10] if stats["oldest"] else "-"
    newest = str(stats["newest"])[:10] if stats["newest"] else "-"
    rows = "\n".join(f"| {k} | {v} |" for k, v in stats["by_status"].items())
    res = "\n".join(f"| {k} | {v} |" for k, v in stats["by_resolution"].items())
    exported = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"""# Dataset: ServiceNow, table {stats["table"]}

Exported from {stats["instance"]} on {exported} by `replaybook servicenow export` through the
Table API (records, sys_journal_field for comments and work notes, sys_audit for state
history). User references are reduced to sys_ids before the raw copy is written; text is
redacted at ingest (emails, public IP addresses, secret-looking strings).

| Measure | Value |
|---|---|
| Tickets | {stats["tickets"]} |
| Date range | {oldest} to {newest} |
| Comments and work notes | {stats["comments"]} |
| State changes (from sys_audit) | {stats["status_changes"]} |
| Tickets with state history | {stats["with_status_history"]} |
| Tickets that waited on the requester (On Hold, Awaiting Caller) | {stats["waited_on_requester"]} |
| Tickets with an effort figure | {stats["with_effort"]} |
| Effort source | {describe(stats["effort_source"])} |

## Status (canonical)

| Status | Tickets |
|---|---|
{rows}

## Resolution

| Resolution | Tickets |
|---|---|
{res}

## What is and is not here

- There is no audit log of tool calls. Expectations are extracted from the text
  (`replaybook build-testset --extract rules` or `model`) and stay unverified until a
  person confirms them.
- States are mapped onto the canonical names. On Hold counts as Waiting on Requester only
  with hold reason Awaiting Caller; awaiting vendor, change or problem is the team's wait.
- Work notes are engineer comments; customer-visible comments are the caller's when their
  user name matches, the assignee's when it matches, "other" otherwise.
- time_worked is the only logged effort ServiceNow has by default; the default effort
  source is status_time (time in In Progress) because most queues do not use it.
"""
