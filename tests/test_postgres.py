"""The Postgres adapter: tickets from warehouse tables through a mapping, with a fake
fetcher standing in for the database. No server is ever opened in tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from replaybook.adapters import postgres
from replaybook.adapters.synthetic import load_tickets
from replaybook.adapters.tabular import MappingError
from replaybook.cli import app

TABLES: dict[str, list[dict[str, Any]]] = {
    "analytics.tickets": [
        {
            "ticket_key": "WH-1",
            "title": "Renew cert for a.example.org",
            "body": "Expires in 9 days",
            "requester": "ana",
            "agent": "bob",
            "opened_at": datetime(2026, 2, 1, 9, tzinfo=UTC),
            "closed_at": datetime(2026, 2, 1, 15, tzinfo=UTC),
            "state": "Done",
            "minutes": 30,
        },
        {
            "ticket_key": "WH-2",
            "title": "Add dee to db-admins",
            "body": "New DBA starting Monday",
            "requester": "cal",
            "agent": "bob",
            "opened_at": datetime(2026, 2, 3, 9, tzinfo=UTC),
            "closed_at": datetime(2026, 2, 5, 9, tzinfo=UTC),
            "state": "Done",
            "minutes": None,
        },
    ],
    "analytics.ticket_comments": [
        {
            "ticket_key": "WH-2",
            "author": "bob",
            "posted_at": datetime(2026, 2, 3, 10, tzinfo=UTC),
            "body": "Approved by whom?",
        },
        {
            "ticket_key": "WH-2",
            "author": "cal",
            "posted_at": datetime(2026, 2, 4, 9, tzinfo=UTC),
            "body": "By the DBA lead.",
        },
    ],
    "analytics.ticket_status_history": [
        {
            "ticket_key": "WH-1",
            "changed_at": datetime(2026, 2, 1, 9, tzinfo=UTC),
            "old_state": None,
            "new_state": "Open",
        },
        {
            "ticket_key": "WH-1",
            "changed_at": datetime(2026, 2, 1, 10, tzinfo=UTC),
            "old_state": "Open",
            "new_state": "In Progress",
        },
        {
            "ticket_key": "WH-1",
            "changed_at": datetime(2026, 2, 1, 15, tzinfo=UTC),
            "old_state": "In Progress",
            "new_state": "Done",
        },
        {
            "ticket_key": "WH-2",
            "changed_at": datetime(2026, 2, 3, 9, tzinfo=UTC),
            "old_state": None,
            "new_state": "Open",
        },
        {
            "ticket_key": "WH-2",
            "changed_at": datetime(2026, 2, 3, 10, tzinfo=UTC),
            "old_state": "Open",
            "new_state": "Pending customer",
        },
        {
            "ticket_key": "WH-2",
            "changed_at": datetime(2026, 2, 4, 9, tzinfo=UTC),
            "old_state": "Pending customer",
            "new_state": "In Progress",
        },
        {
            "ticket_key": "WH-2",
            "changed_at": datetime(2026, 2, 5, 9, tzinfo=UTC),
            "old_state": "In Progress",
            "new_state": "Done",
        },
    ],
    "analytics.ticket_actions": [
        {
            "ticket_key": "WH-2",
            "tool": "platform_group_add_member",
            "args": {"group": "db-admins", "user": "dee"},
            "at": datetime(2026, 2, 5, 8, tzinfo=UTC),
        },
    ],
}


class FakeFetch:
    """Answers queries from the TABLES dict and records what was asked."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.tables = tables if tables is not None else TABLES
        self.calls: list[tuple[str, list[str], str | None, int | None]] = []

    def __call__(
        self, table: str, columns: list[str], where: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        self.calls.append((table, columns, where, limit))
        rows = self.tables.get(table)
        if rows is None:
            raise MappingError(f"relation {table} does not exist")
        return [dict(r) for r in rows]


def _mapping(tmp_path: Path, **overrides: Any) -> Path:
    data: dict[str, Any] = {
        "dsn": "postgresql://replaybook:secret@db.internal:5432/warehouse",
        "source": "warehouse",
        "project": "WH",
        "tickets": {
            "table": "analytics.tickets",
            "columns": {
                "id": "ticket_key",
                "summary": "title",
                "description": "body",
                "reporter": "requester",
                "assignee": "agent",
                "created": "opened_at",
                "resolved": "closed_at",
                "status": "state",
                "time_spent_minutes": "minutes",
            },
            "where": "project = 'WH'",
            "limit": 1000,
        },
        "comments": {
            "table": "analytics.ticket_comments",
            "columns": {
                "ticket_id": "ticket_key",
                "author": "author",
                "created": "posted_at",
                "body": "body",
            },
        },
        "status_changes": {
            "table": "analytics.ticket_status_history",
            "columns": {
                "ticket_id": "ticket_key",
                "at": "changed_at",
                "from_status": "old_state",
                "to_status": "new_state",
            },
        },
        "actions": {
            "table": "analytics.ticket_actions",
            "columns": {"ticket_id": "ticket_key", "tool": "tool", "args": "args", "at": "at"},
        },
        "status_map": {"Pending customer": "Waiting on Requester", "Done": "Resolved"},
        "effort": {"source": "status_time"},
    }
    data.update(overrides)
    path = tmp_path / "pg.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_import_through_a_fake_fetcher(tmp_path: Path) -> None:
    fetch = FakeFetch()
    stats = postgres.import_tickets(_mapping(tmp_path), tmp_path / "out", fetch=fetch)
    assert stats["tickets"] == 2 and stats["history_mapped"]
    assert stats["with_status_history"] == 2 and stats["waited_on_requester"] == 1
    assert stats["with_actions"] == 1 and stats["effort_source"] == "status_time"
    # The ticket query carries the mapping's filter and limit; companions are read whole.
    assert fetch.calls[0] == (
        "analytics.tickets",
        [
            "ticket_key",
            "title",
            "body",
            "requester",
            "agent",
            "opened_at",
            "closed_at",
            "state",
            "minutes",
        ],
        "project = 'WH'",
        1000,
    )
    assert {c[0] for c in fetch.calls[1:]} == {
        "analytics.ticket_comments",
        "analytics.ticket_status_history",
        "analytics.ticket_actions",
    }
    tickets = {t.id: t for t in load_tickets(tmp_path / "out")}
    one, two = tickets["WH-1"], tickets["WH-2"]
    assert one.status == "Resolved" and one.time_spent_hours == 5.0
    assert one.effort_source == "status_time"
    assert [c.to_status for c in two.status_changes] == [
        "Open",
        "Waiting on Requester",
        "In Progress",
        "Resolved",
    ]
    assert [c.role for c in two.comments] == ["engineer", "requester"]
    assert two.actions[0].tool == "platform_group_add_member"
    assert two.actions[0].args == {"group": "db-admins", "user": "dee"}
    card = (tmp_path / "out" / "DATASET.md").read_text(encoding="utf-8")
    assert "status_time" in card and "analytics.tickets" in card
    written = card + (tmp_path / "out" / "tickets.jsonl").read_text(encoding="utf-8")
    assert "replaybook:secret@" not in written, "the connection string never lands on disk"


def test_history_is_required_unless_allowed(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, status_changes=None)
    with pytest.raises(MappingError, match="status-change history"):
        postgres.PostgresMapping.load(mapping)
    stats = postgres.import_tickets(
        mapping, tmp_path / "out", fetch=FakeFetch(), allow_missing_history=True
    )
    assert stats["tickets"] == 2 and not stats["history_mapped"]
    assert stats["waited_on_requester"] == 0 and stats["with_status_history"] == 0
    card = (tmp_path / "out" / "DATASET.md").read_text(encoding="utf-8")
    assert "No status-change history" in card
    tickets = load_tickets(tmp_path / "out")
    assert all(t.time_spent_hours is None for t in tickets), "status_time has nothing to measure"


def test_mapping_validation_and_dsn_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(MappingError, match="identifier"):
        postgres.PostgresMapping.load(
            _mapping(
                tmp_path,
                tickets={"table": "analytics.tickets; drop table x", "columns": {"id": "k"}},
            )
        )
    bad_cols = _mapping(tmp_path)
    data = yaml.safe_load(bad_cols.read_text(encoding="utf-8"))
    data["tickets"]["columns"] = {"id": "ticket_key"}
    bad_cols.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(MappingError, match="missing summary"):
        postgres.PostgresMapping.load(bad_cols)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://from-env/db")
    env_mapping = postgres.PostgresMapping.load(_mapping(tmp_path, dsn=None))
    assert env_mapping.dsn == "postgresql://from-env/db"
    monkeypatch.setenv("WAREHOUSE_DSN", "postgresql://named/db")
    named = postgres.PostgresMapping.load(_mapping(tmp_path, dsn="${WAREHOUSE_DSN}"))
    assert named.dsn == "postgresql://named/db"
    assert postgres.build_query("analytics.tickets", ["a", "b"], "x = 1", 10) == (
        'SELECT "a", "b" FROM "analytics"."tickets" WHERE x = 1 LIMIT 10'
    )
    assert postgres.build_query("t", [], None, None) == 'SELECT * FROM "t"'


def test_cli_reports_and_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres, "psycopg_fetch", lambda dsn: FakeFetch())
    result = CliRunner().invoke(
        app,
        ["postgres", "import", "--mapping", str(_mapping(tmp_path)), "--out", str(tmp_path / "o")],
    )
    assert result.exit_code == 0, result.output
    assert "2 tickets from analytics.tickets" in result.output and "WARNING" not in result.output
    refused = CliRunner().invoke(
        app,
        [
            "postgres",
            "import",
            "--mapping",
            str(_mapping(tmp_path, status_changes=None)),
            "--out",
            str(tmp_path / "o2"),
        ],
    )
    assert refused.exit_code == 2 and "status-change history" in refused.output
    allowed = CliRunner().invoke(
        app,
        [
            "postgres",
            "import",
            "--mapping",
            str(_mapping(tmp_path, status_changes=None)),
            "--out",
            str(tmp_path / "o3"),
            "--allow-missing-history",
        ],
    )
    assert allowed.exit_code == 0, allowed.output
    assert "WARNING: no status-change history" in allowed.output


def test_links_table(tmp_path: Path) -> None:
    tables = dict(TABLES)
    tables["analytics.ticket_links"] = [
        {"ticket_key": "WH-2", "linked_key": "WH-1", "link_type": "Duplicate"},
        {"ticket_key": "WH-1", "linked_key": "OTHER-5", "link_type": "Relates"},
    ]
    links = {
        "table": "analytics.ticket_links",
        "columns": {"ticket_id": "ticket_key", "target": "linked_key", "type": "link_type"},
    }
    fetch = FakeFetch(tables)
    postgres.import_tickets(_mapping(tmp_path, links=links), tmp_path / "out", fetch=fetch)
    assert "analytics.ticket_links" in {c[0] for c in fetch.calls}
    tickets = {t.id: t for t in load_tickets(tmp_path / "out")}
    assert [(x.type, x.target) for x in tickets["WH-2"].links] == [("duplicates", "WH-1")]
    assert [(x.type, x.target) for x in tickets["WH-1"].links] == [("relates", "OTHER-5")]


def test_queries_shape_columns_a_read_only_user_cannot_change(tmp_path: Path) -> None:
    """A RAG store without a reporter column or comment authors: the mapping shapes them in
    a query, because a read-only import cannot create a view."""
    ticket_query = (
        "SELECT ticket_key, title, body, 'unknown' AS reporter, opened_at, state "
        "FROM analytics.tickets"
    )
    comment_query = (
        "SELECT ticket_key, posted_at, body, CASE WHEN author = 'bob' THEN 'engineer' "
        "ELSE 'other' END AS role FROM analytics.ticket_comments"
    )
    tables = {
        ticket_query: [
            {**{k: v for k, v in row.items() if k != "requester"}, "reporter": "unknown"}
            for row in TABLES["analytics.tickets"]
        ],
        comment_query: [
            {
                "ticket_key": "WH-2",
                "posted_at": datetime(2026, 2, 3, 10, tzinfo=UTC),
                "body": "Which environment?",
                "role": "engineer",
            }
        ],
    }
    mapping = _mapping(
        tmp_path,
        tickets={
            "query": ticket_query + ";",
            "columns": {
                "id": "ticket_key",
                "summary": "title",
                "description": "body",
                "reporter": "reporter",
                "created": "opened_at",
                "status": "state",
            },
            "limit": 10,
        },
        comments={
            "query": comment_query,
            "columns": {
                "ticket_id": "ticket_key",
                "created": "posted_at",
                "body": "body",
                "role": "role",
            },
        },
        status_changes=None,
        actions=None,
        allow_missing_history=True,
    )
    fetch = FakeFetch(tables)
    postgres.import_tickets(mapping, tmp_path / "out", fetch=fetch)
    assert fetch.calls[0][0] == ticket_query and fetch.calls[0][3] == 10
    tickets = {t.id: t for t in load_tickets(tmp_path / "out")}
    assert tickets["WH-1"].reporter == "unknown"
    assert [c.role for c in tickets["WH-2"].comments] == ["engineer"]
    assert postgres.build_query(ticket_query, ["title"], "state = 'Done'", 5) == (
        f"SELECT \"title\" FROM ({ticket_query}) AS mapped WHERE state = 'Done' LIMIT 5"
    )
    for bad in (
        {"table": "analytics.tickets", "query": ticket_query},
        {"query": "DELETE FROM analytics.tickets"},
        {"query": ticket_query + "; DROP TABLE x"},
    ):
        with pytest.raises(MappingError, match="table or query|one SELECT"):
            postgres.PostgresMapping.load(
                _mapping(tmp_path, tickets={**bad, "columns": {"id": "k"}})
            )
