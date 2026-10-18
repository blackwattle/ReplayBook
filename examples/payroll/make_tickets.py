"""Writes examples/payroll/tickets.jsonl: a dozen canonical tickets for the conformance
test, as a payroll team's ticket system might have recorded them. Deterministic; run it
again and the file is the same. Everything is made up."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "tickets.jsonl"
START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

RATE_CHANGES = [
    ("E1001", "Jordan Lee", 40.0, 45.5, "2026-03-01"),
    ("E1002", "Sam Patel", 52.0, 55.0, "2026-03-01"),
    ("E1003", "Ana Costa", 38.0, 41.0, None),
    ("E1004", "Kai Nguyen", 61.0, 64.5, "2026-04-01"),
    ("E1005", "Dee Okafor", 44.0, 46.0, "2026-04-15"),
    ("E1006", "Max Rossi", 35.5, 37.0, None),
    ("E1007", "Lena Silva", 48.0, 50.0, "2026-05-01"),
    ("E1008", "Omar Haddad", 42.0, 44.0, "2026-05-01"),
    ("E1009", "Pia Schulz", 39.0, 42.5, None),
    ("E1010", "Ravi Iyer", 57.0, 60.0, "2026-06-01"),
]
# Tickets where the requester left the new rate out and the engineer had to ask.
ASK_FOR_RATE = {"E1005", "E1009"}


def stamp(at: datetime) -> str:
    return at.isoformat()


def ticket(
    index: int, employee: str, name: str, old: float, new: float, effective: str | None
) -> dict[str, Any]:
    created = START + timedelta(days=index * 6, hours=index % 5)
    asked = employee in ASK_FOR_RATE
    rate_text = f"${new:.2f} per hour" if not asked else "the agreed new rate"
    when = f" effective from {effective}" if effective else ""
    summary = f"Pay rate change for {employee} ({name})"
    description = (
        f"Please update {employee}'s hourly rate to {rate_text}{when}. "
        f"Approved by their manager; see the approval attached to this ticket."
    )
    comments: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = [
        {"at": stamp(created), "from_status": None, "to_status": "Open"}
    ]
    now = created + timedelta(hours=2)
    changes.append({"at": stamp(now), "from_status": "Open", "to_status": "In Progress"})
    if asked:
        now += timedelta(hours=1)
        comments.append(
            {
                "author": "payroll-eng-1",
                "role": "engineer",
                "created": stamp(now),
                "body": f"What is the new hourly rate for {employee}? It is not in the ticket.",
            }
        )
        changes.append(
            {"at": stamp(now), "from_status": "In Progress", "to_status": "Waiting on Requester"}
        )
        now += timedelta(days=1, hours=3)
        comments.append(
            {
                "author": "hr.partner",
                "role": "requester",
                "created": stamp(now),
                "body": f"Sorry, it is ${new:.2f} per hour.",
            }
        )
        changes.append(
            {"at": stamp(now), "from_status": "Waiting on Requester", "to_status": "In Progress"}
        )
    now += timedelta(hours=1)
    args: dict[str, Any] = {"employee_id": employee, "hourly_rate": new}
    if effective:
        args["effective_from"] = effective
    actions = [
        {"tool": "payroll_set_pay_rate", "args": args, "at": stamp(now), "source": "audit_log"}
    ]
    now += timedelta(minutes=20)
    comments.append(
        {
            "author": "payroll-eng-1",
            "role": "engineer",
            "created": stamp(now),
            "body": "Done, rate updated.",
        }
    )
    changes.append({"at": stamp(now), "from_status": "In Progress", "to_status": "Resolved"})
    return {
        "id": f"PAY-{100 + index}",
        "source": "payroll_example",
        "project": "PAY",
        "summary": summary,
        "description": description,
        "reporter": "hr.partner",
        "assignee": "payroll-eng-1",
        "created": stamp(created),
        "resolved": stamp(now),
        "status": "Resolved",
        "resolution": "Done",
        "priority": "Medium",
        "labels": ["payroll"],
        "components": ["payroll"],
        "comments": comments,
        "status_changes": changes,
        "code_changes": [],
        "actions": actions,
        "links": [],
        "time_spent_hours": 0.5 if index % 2 else None,
        "effort_source": "logged" if index % 2 else None,
        "inventory": {
            "payroll": {
                "reads": {
                    "payroll_get_employee": {
                        "employee_id": employee,
                        "name": name,
                        "hourly_rate": old,
                        "cost_centre": f"CC-{10 + index}",
                    }
                }
            }
        },
    }


def noise(index: int, summary: str, description: str) -> dict[str, Any]:
    created = START + timedelta(days=index * 6 + 3)
    done = created + timedelta(hours=5)
    return {
        "id": f"PAY-{100 + index}",
        "source": "payroll_example",
        "project": "PAY",
        "summary": summary,
        "description": description,
        "reporter": "someone",
        "assignee": "payroll-eng-2",
        "created": stamp(created),
        "resolved": stamp(done),
        "status": "Resolved",
        "resolution": "Done",
        "priority": "Low",
        "labels": [],
        "components": [],
        "comments": [
            {
                "author": "payroll-eng-2",
                "role": "engineer",
                "created": stamp(done),
                "body": "Closing.",
            }
        ],
        "status_changes": [
            {"at": stamp(created), "from_status": None, "to_status": "Open"},
            {"at": stamp(done), "from_status": "Open", "to_status": "Resolved"},
        ],
        "code_changes": [],
        "actions": [],
        "links": [],
        "time_spent_hours": 0.25,
        "effort_source": "logged",
        "inventory": {},
    }


CSV_DIR = OUT.parent / "csv"


def write_csv_form(rows: list[dict[str, Any]]) -> None:
    """The same tickets as a ticket system would export them: one table per thing plus
    the mapping that turns them back into canonical tickets (see docs/ticket-sources.md)."""
    import csv

    CSV_DIR.mkdir(exist_ok=True)
    with (CSV_DIR / "tickets.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Issue key",
                "Summary",
                "Description",
                "Reporter",
                "Assignee",
                "Created",
                "Resolved",
                "Status",
                "Resolution",
                "Priority",
                "Labels",
                "Components",
                "Time spent (h)",
                "Inventory",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["id"],
                    r["summary"],
                    r["description"],
                    r["reporter"],
                    r["assignee"] or "",
                    r["created"],
                    r["resolved"],
                    r["status"],
                    r["resolution"],
                    r["priority"],
                    ";".join(r["labels"]),
                    ";".join(r["components"]),
                    "" if r["time_spent_hours"] is None else r["time_spent_hours"],
                    json.dumps(r["inventory"], sort_keys=True) if r["inventory"] else "",
                ]
            )
    with (CSV_DIR / "comments.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Issue key", "Author", "Created", "Body"])
        for r in rows:
            for c in r["comments"]:
                writer.writerow([r["id"], c["author"], c["created"], c["body"]])
    with (CSV_DIR / "history.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Issue key", "Changed", "From", "To"])
        for r in rows:
            for c in r["status_changes"]:
                writer.writerow([r["id"], c["at"], c["from_status"] or "", c["to_status"]])
    with (CSV_DIR / "actions.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for r in rows:
            for a in r["actions"]:
                handle.write(json.dumps({"ticket_id": r["id"], **a}, sort_keys=True))
                handle.write("\n")
    (CSV_DIR / "mapping.yaml").write_text(
        """# Column mapping for the payroll example export (docs/ticket-sources.md).
source: payroll_example
project: PAY
tickets:
  file: tickets.csv
  columns:
    id: Issue key
    summary: Summary
    description: Description
    reporter: Reporter
    assignee: Assignee
    created: Created
    resolved: Resolved
    status: Status
    resolution: Resolution
    priority: Priority
    labels: Labels
    components: Components
    time_spent_hours: Time spent (h)
    inventory: Inventory
  list_separator: ";"
comments:
  file: comments.csv
  columns: {ticket_id: Issue key, author: Author, created: Created, body: Body}
status_changes:
  file: history.csv
  columns: {ticket_id: Issue key, at: Changed, from_status: From, to_status: To}
actions:
  file: actions.jsonl
  columns: {ticket_id: ticket_id, tool: tool, args: args, at: at}
engineers: [payroll-eng-1, payroll-eng-2]
effort:
  source: logged
""",
        encoding="utf-8",
    )


def main() -> None:
    rows = [ticket(i, *change) for i, change in enumerate(RATE_CHANGES)]
    rows.append(
        noise(10, "Payslip PDF will not download", "The March payslip PDF gives a 500 error.")
    )
    rows.append(
        noise(11, "Question about leave accrual", "How is annual leave accrued for part-timers?")
    )
    rows.sort(key=lambda r: r["created"])
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")
    write_csv_form(rows)
    print(f"wrote {len(rows)} tickets to {OUT} and the CSV form to {CSV_DIR}")


if __name__ == "__main__":
    main()
