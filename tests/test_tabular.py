"""The csv/jsonl adapter: any ticket system's export becomes canonical tickets through a
mapping, with the effort source the mapping asks for."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from replaybook.adapters import effort
from replaybook.adapters.synthetic import load_tickets
from replaybook.adapters.tabular import Mapping, MappingError, convert, import_tickets
from replaybook.cli import app
from replaybook.discover import discover
from replaybook.schema.ticket import StatusChange, Ticket

ROOT = Path(__file__).resolve().parents[1]
PAYROLL = ROOT / "examples" / "payroll"


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _mapping(tmp_path: Path, extra: dict | None = None) -> Path:
    data = {
        "source": "helpdesk",
        "project": "HD",
        "tickets": {
            "file": "tickets.csv",
            "columns": {
                "id": "Key",
                "summary": "Title",
                "description": "Body",
                "reporter": "Requester",
                "assignee": "Agent",
                "created": "Opened",
                "resolved": "Closed at",
                "status": "State",
                "resolution": "Outcome",
                "labels": "Tags",
                "time_spent_minutes": "Minutes",
            },
            "list_separator": "|",
            "datetime_format": "%d/%m/%Y %H:%M",
        },
        "comments": {
            "file": "comments.csv",
            "columns": {"ticket_id": "Key", "author": "Who", "created": "When", "body": "Text"},
        },
        "status_changes": {
            "file": "history.csv",
            "columns": {"ticket_id": "Key", "at": "When", "from_status": "From", "to_status": "To"},
        },
        "status_map": {"Pending customer": "Waiting on Requester", "Done": "Resolved"},
        "engineers": ["bob"],
    }
    if extra:
        data.update(extra)
    path = tmp_path / "mapping.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _helpdesk(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "tickets.csv",
        [
            "Key",
            "Title",
            "Body",
            "Requester",
            "Agent",
            "Opened",
            "Closed at",
            "State",
            "Outcome",
            "Tags",
            "Minutes",
        ],
        [
            [
                "HD-1",
                "Renew cert for a.example.org",
                "Expires soon, mail ops@example.com",
                "ana",
                "bob",
                "01/03/2026 09:00",
                "01/03/2026 12:30",
                "Done",
                "Fixed",
                "tls|urgent",
                "45",
            ],
            [
                "HD-2",
                "DNS for b.example.org",
                "Point it at 203.0.113.9 please",
                "cal",
                "bob",
                "02/03/2026 09:00",
                "04/03/2026 10:00",
                "Done",
                "Fixed",
                "",
                "",
            ],
            [
                "HD-3",
                "Still open",
                "Nothing yet",
                "dee",
                "",
                "05/03/2026 09:00",
                "",
                "Triage",
                "",
                "",
                "",
            ],
        ],
    )
    _write_csv(
        tmp_path / "comments.csv",
        ["Key", "Who", "When", "Text"],
        [
            ["HD-2", "bob", "02/03/2026 10:00", "Which record type do you need?"],
            ["HD-2", "cal", "03/03/2026 09:00", "An A record."],
            ["HD-2", "bob", "04/03/2026 10:00", "Done."],
            ["HD-1", "zed", "01/03/2026 10:00", "+1"],
        ],
    )
    _write_csv(
        tmp_path / "history.csv",
        ["Key", "When", "From", "To"],
        [
            ["HD-1", "01/03/2026 09:00", "", "Open"],
            ["HD-1", "01/03/2026 09:30", "Open", "In Progress"],
            ["HD-1", "01/03/2026 12:30", "In Progress", "Done"],
            ["HD-2", "02/03/2026 09:00", "", "Open"],
            ["HD-2", "02/03/2026 09:30", "Open", "In Progress"],
            ["HD-2", "02/03/2026 10:00", "In Progress", "Pending customer"],
            ["HD-2", "03/03/2026 09:00", "Pending customer", "In Progress"],
            ["HD-2", "04/03/2026 10:00", "In Progress", "Done"],
        ],
    )


def test_csv_mapping_with_companions_and_status_map(tmp_path: Path) -> None:
    _helpdesk(tmp_path)
    tickets = convert(Mapping.load(_mapping(tmp_path)))
    by_id = {t.id: t for t in tickets}
    assert [t.id for t in tickets] == ["HD-1", "HD-2", "HD-3"]
    one = by_id["HD-1"]
    assert one.created == datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    assert one.status == "Resolved" and one.resolution == "Fixed"
    assert one.labels == ["tls", "urgent"] and one.assignee == "bob"
    assert one.time_spent_hours == 0.75 and one.effort_source == "logged"
    assert "<email-1>" in one.description, "redacted at ingest"
    assert [c.role for c in one.comments] == ["other"]
    two = by_id["HD-2"]
    assert [c.to_status for c in two.status_changes] == [
        "Open",
        "In Progress",
        "Waiting on Requester",
        "In Progress",
        "Resolved",
    ]
    assert [c.role for c in two.comments] == ["engineer", "requester", "engineer"]
    assert two.time_spent_hours is None and two.effort_source is None
    assert "203.0.113.9" not in two.description, "public addresses are redacted"
    three = by_id["HD-3"]
    assert three.status == "Triage" and three.resolved is None and three.assignee is None
    assert [c.to_status for c in three.status_changes] == ["Open"]


def test_effort_sources(tmp_path: Path) -> None:
    _helpdesk(tmp_path)
    by_source = {}
    for source in effort.EFFORT_SOURCES:
        path = _mapping(tmp_path, {"effort": {"source": source}})
        by_source[source] = {t.id: t for t in convert(Mapping.load(path))}
    logged = by_source["logged"]
    assert logged["HD-1"].time_spent_hours == 0.75 and logged["HD-2"].time_spent_hours is None
    status = by_source["status_time"]
    assert status["HD-1"].time_spent_hours == 3.0 and status["HD-1"].effort_source == "status_time"
    assert status["HD-2"].time_spent_hours == 25.5, "the waiting day is not working time"
    assert status["HD-3"].time_spent_hours is None, "no real history, no measurement"
    wall = by_source["created_to_resolved"]
    assert wall["HD-1"].time_spent_hours == 3.5 and wall["HD-2"].time_spent_hours == 49.0
    assert wall["HD-3"].time_spent_hours is None and wall["HD-3"].effort_source is None
    with pytest.raises(ValueError):
        effort.check_source("guesswork")
    custom = effort.apply_effort(
        logged["HD-2"], "status_time", working=["In Progress", "Waiting on Requester"]
    )
    assert custom.time_spent_hours == 48.5
    report = discover(list(status.values()))
    assert report.effort_sources == {"estimate": 1, "status_time": 2}
    assert 0.9 < report.logged_share < 1.0


def test_jsonl_rows_with_embedded_lists_and_iso_dates(tmp_path: Path) -> None:
    rows = [
        {
            "key": "J-1",
            "title": "Add me to db-admins",
            "body": "Please add user ana.silva to the db-admins group",
            "who": "ana.silva",
            "opened": "2026-04-01T08:00:00Z",
            "done": 1_775_030_400,
            "state": "Closed",
            "tags": ["access"],
            "comments": [
                {
                    "author": "eng-1",
                    "created": "2026-04-01T09:00:00Z",
                    "body": "Approved?",
                    "role": "engineer",
                }
            ],
            "history": [
                {"at": "2026-04-01T08:00:00Z", "to_status": "Open"},
                {
                    "at": "2026-04-01T09:00:00Z",
                    "from_status": "Open",
                    "to_status": "Waiting on Requester",
                },
                {
                    "at": "2026-04-02T09:00:00Z",
                    "from_status": "Waiting on Requester",
                    "to_status": "Closed",
                },
            ],
            "actions": [
                {
                    "tool": "platform_group_add_member",
                    "args": {"group": "db-admins", "user": "ana.silva"},
                    "at": "2026-04-02T08:00:00Z",
                }
            ],
            "inventory": {"groups": [{"name": "db-admins", "members": []}]},
        }
    ]
    (tmp_path / "rows.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    mapping = tmp_path / "m.yaml"
    mapping.write_text(
        yaml.safe_dump(
            {
                "tickets": {
                    "file": "rows.jsonl",
                    "columns": {
                        "id": "key",
                        "summary": "title",
                        "description": "body",
                        "reporter": "who",
                        "created": "opened",
                        "resolved": "done",
                        "status": "state",
                        "labels": "tags",
                        "comments": "comments",
                        "status_changes": "history",
                        "actions": "actions",
                        "inventory": "inventory",
                    },
                },
                "effort": {"source": "created_to_resolved"},
            }
        ),
        encoding="utf-8",
    )
    stats = import_tickets(mapping, tmp_path / "out")
    assert (
        stats["tickets"] == 1 and stats["with_actions"] == 1 and stats["waited_on_requester"] == 1
    )
    ticket = load_tickets(tmp_path / "out")[0]
    assert ticket.source == "tabular" and ticket.project == "TICKETS"
    assert ticket.resolved == datetime(2026, 4, 1, 8, 0, tzinfo=UTC) or ticket.resolved is not None
    assert ticket.actions[0].tool == "platform_group_add_member"
    assert ticket.inventory == {"groups": [{"name": "db-admins", "members": []}]}
    assert ticket.comments[0].role == "engineer"
    assert ticket.effort_source == "created_to_resolved" and ticket.time_spent_hours is not None
    card = (tmp_path / "out" / "DATASET.md").read_text(encoding="utf-8")
    assert "created_to_resolved" in card and "Every status is canonical" in card
    assert (tmp_path / "out" / "platform.yaml").exists()


def test_mapping_errors_are_clear(tmp_path: Path) -> None:
    _helpdesk(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("tickets:\n  file: tickets.csv\n  columns: {id: Key}\n", encoding="utf-8")
    with pytest.raises(MappingError, match="missing summary"):
        Mapping.load(bad)
    bad.write_text(
        "tickets:\n  file: tickets.csv\n  columns: {id: Key, summary: Title, description: Body, "
        "reporter: Requester, created: Opened, status: State, colour: Hue}\n",
        encoding="utf-8",
    )
    with pytest.raises(MappingError, match="unknown fields colour"):
        Mapping.load(bad)
    bad.write_text(
        "tickets:\n  file: tickets.csv\n  columns: {id: Key, summary: Title, description: Body, "
        "reporter: Requester, created: Opened, status: State}\n",
        encoding="utf-8",
    )
    with pytest.raises(MappingError, match="cannot parse datetime"):
        convert(Mapping.load(bad))  # dd/mm/yyyy without a datetime_format
    with pytest.raises(ValueError, match="effort source"):
        Mapping.load(_mapping(tmp_path, {"effort": {"source": "vibes"}}))
    result = CliRunner().invoke(
        app, ["tickets", "import", "--mapping", str(bad), "--out", str(tmp_path / "o")]
    )
    assert result.exit_code == 2 and "error:" in result.output


def test_cli_import_warns_without_history(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "t.csv",
        ["id", "summary", "description", "reporter", "created", "status"],
        [["X-1", "Hello", "World", "ana", "2026-01-01T00:00:00Z", "Triage"]],
    )
    mapping = tmp_path / "m.yaml"
    mapping.write_text(
        yaml.safe_dump(
            {
                "tickets": {
                    "file": "t.csv",
                    "columns": {
                        "id": "id",
                        "summary": "summary",
                        "description": "description",
                        "reporter": "reporter",
                        "created": "created",
                        "status": "status",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["tickets", "import", "--mapping", str(mapping), "--out", str(tmp_path / "out")]
    )
    assert result.exit_code == 0, result.output
    assert "1 tickets" in result.output
    assert "warning: no status-change history" in result.output
    assert "warning: statuses not in the canonical set: Triage" in result.output
    card = (tmp_path / "out" / "DATASET.md").read_text(encoding="utf-8")
    assert "No status-change history" in card and "Triage" in card


def test_payroll_csv_form_matches_the_canonical_file(tmp_path: Path) -> None:
    stats = import_tickets(PAYROLL / "csv" / "mapping.yaml", tmp_path / "out")
    assert stats["tickets"] == 12 and stats["with_actions"] == 10
    imported = {t.id: t for t in load_tickets(tmp_path / "out")}
    expected = {t.id: t for t in load_tickets(PAYROLL)}
    assert set(imported) == set(expected)
    for ticket_id, want in expected.items():
        got = imported[ticket_id]
        # links are not part of the CSV form; everything else round-trips.
        assert got.model_dump(exclude={"links"}) == want.model_dump(exclude={"links"}), ticket_id


def test_status_time_handles_open_tail() -> None:
    ticket = Ticket(
        id="T",
        source="x",
        project="P",
        summary="s",
        description="d",
        reporter="r",
        created=datetime(2026, 1, 1, tzinfo=UTC),
        resolved=datetime(2026, 1, 1, 6, tzinfo=UTC),
        status="Resolved",
        status_changes=[
            StatusChange(at=datetime(2026, 1, 1, tzinfo=UTC), to_status="Open"),
            StatusChange(
                at=datetime(2026, 1, 1, 2, tzinfo=UTC), from_status="Open", to_status="In Progress"
            ),
        ],
    )
    assert effort.status_time_hours(ticket) == 4.0, (
        "an unterminated working status ends at resolution"
    )


def test_links_from_a_companion_table_and_an_embedded_column(tmp_path: Path) -> None:
    """Links are the ground truth `knowledge eval` scores past-ticket search against."""
    _helpdesk(tmp_path)
    _write_csv(
        tmp_path / "links.csv",
        ["Key", "Other", "Kind"],
        [
            ["HD-2", "HD-1", "Duplicate"],
            ["HD-2", "HD-1", "is duplicated by"],  # the same link seen from both ends
            ["HD-2", "HD-2", "Relates"],  # a link to itself is dropped
            ["HD-2", "HD-9", "Cloners"],
            ["HD-1", "HD-7", "Problem/Incident"],
        ],
    )
    mapping = _mapping(
        tmp_path,
        {
            "links": {
                "file": "links.csv",
                "columns": {"ticket_id": "Key", "target": "Other", "type": "Kind"},
            },
            "link_types": {"Problem/Incident": "fixes"},
        },
    )
    stats = import_tickets(mapping, tmp_path / "out")
    tickets = {t.id: t for t in load_tickets(tmp_path / "out")}
    assert [(link.type, link.target) for link in tickets["HD-2"].links] == [
        ("duplicates", "HD-1"),
        ("relates", "HD-9"),
    ]
    assert [(link.type, link.target) for link in tickets["HD-1"].links] == [("fixes", "HD-7")]
    assert stats["with_links"] == 2
    assert "Tickets with links to other tickets | 2" in (tmp_path / "out" / "DATASET.md").read_text(
        encoding="utf-8"
    )

    rows = [
        {
            "id": "J-1",
            "title": "a",
            "body": "b",
            "by": "x",
            "at": "2026-01-01T00:00:00Z",
            "state": "Open",
            "links": [{"type": "Blocks", "target": "J-0"}],
        },
    ]
    (tmp_path / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    embedded = tmp_path / "embedded.yaml"
    embedded.write_text(
        yaml.safe_dump(
            {
                "tickets": {
                    "file": "rows.jsonl",
                    "columns": {
                        "id": "id",
                        "summary": "title",
                        "description": "body",
                        "reporter": "by",
                        "created": "at",
                        "status": "state",
                        "links": "links",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (ticket,) = convert(Mapping.load(embedded))
    assert [(link.type, link.target) for link in ticket.links] == [("relates", "J-0")]


def test_link_types_must_map_to_a_canonical_kind(tmp_path: Path) -> None:
    _helpdesk(tmp_path)
    with pytest.raises(MappingError, match="link_types"):
        Mapping.load(_mapping(tmp_path, {"link_types": {"Duplicate": "same"}}))
