"""The ServiceNow adapter against fixtures built from the documented Table API shapes."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from typer.testing import CliRunner

from replaybook.adapters import servicenow
from replaybook.adapters.servicenow import (
    ServiceNow,
    ServiceNowConfig,
    ServiceNowError,
    canonical_state,
    duration_hours,
    strip_users,
)
from replaybook.adapters.synthetic import load_tickets
from replaybook.cli import app
from replaybook.eval.testset import build_test_set

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "servicenow"
CONFIG = ServiceNowConfig("https://dev00000.service-now.com", "replaybook", "secret-pass")


class FakeInstance:
    """Serves the fixtures the way the Table API would: pages by offset, filters the
    journal and audit tables by the ids in the query, and records every request."""

    def __init__(self, page_size_cap: int | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.tables = {
            name: json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["result"]
            for name in ("incident", "sys_journal_field", "sys_audit")
        }
        self.page_size_cap = page_size_cap

    def __call__(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        parts = urlsplit(url)
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((parts.path, params, headers))
        table = parts.path.rsplit("/", 1)[-1]
        rows = list(self.tables.get(table, []))
        query = params.get("sysparm_query", "")
        for clause in query.split("^"):
            if "IN" in clause and not clause.startswith("ORDERBY"):
                field, ids = clause.split("IN", 1)
                wanted = set(ids.split(","))
                rows = [r for r in rows if r.get(field, {}).get("value") in wanted]
        limit = int(params.get("sysparm_limit", 200))
        if self.page_size_cap:
            limit = min(limit, self.page_size_cap)
        offset = int(params.get("sysparm_offset", 0))
        return {"result": rows[offset : offset + limit]}


def test_export_converts_incidents_with_journal_and_audit(tmp_path: Path) -> None:
    fake = FakeInstance()
    client = ServiceNow(CONFIG, fetch=fake)
    stats = client.export_table("incident", tmp_path / "out")
    assert stats["tickets"] == 4 and stats["raw_documents"] == 4
    assert stats["effort_source"] == "status_time" and stats["waited_on_requester"] == 2
    assert fake.calls[0][2]["Authorization"].startswith("Basic ")
    assert fake.calls[0][1]["sysparm_display_value"] == "all"
    assert "caller_id.user_name" in fake.calls[0][1]["sysparm_fields"]
    journal_query = next(
        p["sysparm_query"] for path, p, _ in fake.calls if path.endswith("sys_journal_field")
    )
    assert "name=incident" in journal_query and "element_idIN" in journal_query
    audit_query = next(
        p["sysparm_query"] for path, p, _ in fake.calls if path.endswith("sys_audit")
    )
    assert "tablename=incident" in audit_query and "fieldnameINstate,hold_reason" in audit_query

    tickets = {t.id: t for t in load_tickets(tmp_path / "out")}
    one = tickets["INC0010001"]
    assert one.status == "Closed" and one.resolution == "Done"
    assert one.created == datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    assert one.resolved == datetime(2026, 3, 2, 12, 30, tzinfo=UTC)
    assert one.reporter == "u-abel" and one.assignee == "u-beth", "sys_ids, never names"
    assert "<email-1>" in one.description
    assert [c.to_status for c in one.status_changes] == [
        "Open",
        "In Progress",
        "Resolved",
        "Closed",
    ]
    assert [c.role for c in one.comments] == ["engineer"], "work notes are engineer comments"
    assert one.labels == ["Network", "TLS"] and one.components == ["Platform"]
    assert one.priority == "Moderate"
    assert one.time_spent_hours == 3.0 and one.effort_source == "status_time"

    two = tickets["INC0010002"]
    assert [c.to_status for c in two.status_changes] == [
        "Open",
        "In Progress",
        "Waiting on Requester",
        "In Progress",
        "Resolved",
    ]
    assert [c.role for c in two.comments] == ["engineer", "requester", "engineer", "other"]
    assert two.resolution == "Done"
    waiting = next(c for c in two.status_changes if c.to_status == "Waiting on Requester")
    assert waiting.at == datetime(2026, 3, 3, 10, 0, tzinfo=UTC)

    three = tickets["INC0010003"]
    assert three.status == "Closed" and three.resolution == "Canceled" and three.assignee is None
    four = tickets["INC0010004"]
    assert (
        four.status == "Waiting on Requester" and four.resolved is None and four.resolution is None
    )

    raw = [
        json.loads(line) for line in gzip.open(tmp_path / "out" / "raw" / "incident.jsonl.gz", "rt")
    ]
    assert raw[0]["record"]["caller_id"] == {"value": "u-abel"}, "display names stripped"
    assert "caller_id.user_name" not in raw[0]["record"]
    written = (tmp_path / "out" / "tickets.jsonl").read_text(encoding="utf-8")
    assert "Abel Tuter" not in written and "secret-pass" not in written
    card = (tmp_path / "out" / "DATASET.md").read_text(encoding="utf-8")
    assert "status_time" in card and "Awaiting Caller" in card
    # An ask case comes out of the history: INC0010002 waited on the caller after a question.
    ts = build_test_set(
        list(tickets.values()), {}, "dns_change", n=10, source="servicenow", extractor=None
    )
    assert ts.cases == [], "no audit log of tool calls, so nothing without an extractor"


def test_paging_and_logged_effort(tmp_path: Path) -> None:
    fake = FakeInstance(page_size_cap=2)
    client = ServiceNow(CONFIG, fetch=fake)
    records = list(client.records("incident", fields="sys_id,number", page_size=2))
    assert [r["number"]["value"] for r in records] == [
        "INC0010001",
        "INC0010002",
        "INC0010003",
        "INC0010004",
    ]
    offsets = [p["sysparm_offset"] for path, p, _ in fake.calls if path.endswith("/incident")]
    assert offsets == ["0", "2", "4"]
    stats = client.export_table("incident", tmp_path / "logged", effort="logged")
    tickets = {t.id: t for t in load_tickets(tmp_path / "logged")}
    assert tickets["INC0010001"].time_spent_hours == 0.75
    assert tickets["INC0010001"].effort_source == "logged"
    assert tickets["INC0010002"].time_spent_hours is None
    assert stats["with_effort"] == 1


def test_helpers_and_config() -> None:
    assert canonical_state("incident", "On Hold", "1") == "Waiting on Requester"
    assert canonical_state("incident", "On Hold", "4") == "In Progress"
    assert canonical_state("sc_req_item", "Closed Complete") == "Resolved"
    assert canonical_state("incident", "Weird") == "Weird"
    assert duration_hours("1970-01-01 02:30:00") == 2.5 and duration_hours("") is None
    assert strip_users(
        {
            "opened_by": {"display_value": "X", "value": "u1"},
            "opened_by.user_name": {"value": "x"},
            "number": {"value": "1"},
        }
    ) == {
        "opened_by": {"value": "u1"},
        "number": {"value": "1"},
    }
    with pytest.raises(ServiceNowError, match="table must be one of"):
        ServiceNow(CONFIG, fetch=FakeInstance()).export_table("cmdb_ci", Path("x"))


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ServiceNowError, match="SERVICENOW_INSTANCE"):
        ServiceNowConfig.from_env()
    monkeypatch.setenv("SERVICENOW_INSTANCE", "dev12345.service-now.com/")
    monkeypatch.setenv("SERVICENOW_USER", "u")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "p")
    config = ServiceNowConfig.from_env()
    assert config.instance == "https://dev12345.service-now.com"


def test_cli_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICENOW_INSTANCE", "https://dev12345.service-now.com")
    monkeypatch.setenv("SERVICENOW_USER", "u")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "p")
    fake = FakeInstance()
    monkeypatch.setattr(
        servicenow.ServiceNow, "_http_get", lambda self, url, headers: fake(url, headers)
    )
    result = CliRunner().invoke(
        app, ["servicenow", "export", "--table", "incident", "--out", str(tmp_path / "snow")]
    )
    assert result.exit_code == 0, result.output
    assert "incident: 4 tickets" in result.output and "status_time" in result.output
    assert (tmp_path / "snow" / "tickets.jsonl").exists()
    bad = CliRunner().invoke(
        app, ["servicenow", "export", "--table", "cmdb_ci", "--out", str(tmp_path / "x")]
    )
    assert bad.exit_code == 2 and "table must be one of" in bad.output
