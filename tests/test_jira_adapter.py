"""Jira issue documents (the Public Jira Dataset and Jira Cloud share the shape) become
canonical, redacted tickets; `export` keeps one project with a data card."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from replaybook.adapters import public_jira
from replaybook.adapters.jira_issue import (
    canonical_resolution,
    canonical_status,
    issue_to_ticket,
    parse_jira_datetime,
    text_of,
)
from replaybook.adapters.synthetic import load_tickets

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "jira" / "issues.jsonl"


def _issues() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


def test_issue_to_ticket_maps_fields_roles_statuses_links_and_code_changes() -> None:
    issue = next(i for i in _issues() if i["key"] == "INFRA-1001")
    ticket = issue_to_ticket(issue, "public_jira")
    assert ticket.id == "INFRA-1001" and ticket.project == "INFRA"
    assert ticket.reporter == "u-reporter-1" and ticket.assignee == "u-infra-1"
    assert ticket.created == datetime(2024, 3, 1, 9, 0, tzinfo=UTC)
    assert ticket.resolved == datetime(2024, 3, 2, 15, 30, tzinfo=UTC)
    assert ticket.status == "Closed" and ticket.resolution == "Done"
    assert ticket.priority == "Major" and ticket.labels == ["dns"] and ticket.components == ["DNS"]
    assert ticket.time_spent_hours == 0.5
    assert [c.role for c in ticket.comments] == ["requester", "engineer", "requester", "bot"]
    assert [c.to_status for c in ticket.status_changes] == [
        "Waiting on Requester",
        "In Progress",
        "Closed",
    ]
    assert ticket.status_changes[0].from_status == "Open"
    assert [(link.type, link.target) for link in ticket.links] == [
        ("duplicates", "INFRA-900"),
        ("relates", "INFRA-950"),
    ]
    assert len(ticket.code_changes) == 1
    assert ticket.code_changes[0].repo == "apache/infrastructure-dns"
    assert ticket.code_changes[0].ref == "!412"
    assert ticket.actions == []
    # What the agent would see stops before the first engineer reply.
    assert [c.role for c in ticket.comments_before_first_engineer_reply()] == ["requester"]


def test_engineers_learnt_from_the_project_outrank_the_default_roles() -> None:
    issue = next(i for i in _issues() if i["key"] == "INFRA-1002")
    without = issue_to_ticket(issue, "public_jira")
    assert without.comments[0].role == "engineer"  # the assignee commented
    other = issue_to_ticket(issue, "public_jira", engineers={"u-reporter-2"})
    assert other.comments[0].role == "engineer"
    swapped = dict(issue)
    swapped["fields"] = {**issue["fields"], "assignee": None}
    swapped["changelog"] = {"histories": []}
    assert issue_to_ticket(swapped, "public_jira").comments[0].role == "other"
    assert issue_to_ticket(swapped, "public_jira", {"u-infra-2"}).comments[0].role == "engineer"


def test_dataset_placeholders_and_flattened_comments() -> None:
    """The Public Jira Dataset wraps anonymised ids and flattens comments to fields.comments."""
    issue = next(i for i in _issues() if i["key"] == "INFRA-1002")
    fields = dict(issue["fields"])
    fields["reporter"] = {
        "self": "<<|author_self|>>",
        "name": "<<|author_name|f0e2cc3c-b5d4-4263-a6db-c307bdf50bc8|>>",
        "key": "<<|author_key|9e635d4c-304f-4155-a1f8-5abe2a462976|>>",
        "displayName": "<<|author_displayName|ecad61f5-b537-4989-abe3-d5fb309e0723|>>",
    }
    fields["assignee"] = None
    fields["comments"] = [
        {
            "author": {"key": "<<|author_key|9e635d4c-304f-4155-a1f8-5abe2a462976|>>"},
            "created": "2024-03-20T09:00:00.000+0000",
            "body": "Any news?",
        }
    ]
    fields["comment"] = None
    ticket = issue_to_ticket({**issue, "fields": fields}, "public_jira")
    assert ticket.reporter == "9e635d4c-304f-4155-a1f8-5abe2a462976"
    assert len(ticket.comments) == 1 and ticket.comments[0].role == "requester"
    assert ticket.comments[0].author == ticket.reporter


def test_atlassian_document_format_and_account_ids_are_handled() -> None:
    issue = next(i for i in _issues() if i["key"] == "INFRA-1003")
    ticket = issue_to_ticket(issue, "jira_cloud")
    assert "add user jdoe to the LDAP group hadoop-committers." in ticket.description
    assert "PMC vote" in ticket.description
    assert ticket.reporter == "acc-reporter-3" and ticket.assignee is None
    assert ticket.comments[0].role == "engineer" and "Whimsy" in ticket.comments[0].body
    assert ticket.resolution == "Won't Fix"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Waiting for user", "Waiting on Requester"),
        ("waiting on reporter", "Waiting on Requester"),
        ("Waiting for Infra", "In Progress"),
        ("Resolved", "Resolved"),
        ("Done", "Closed"),
        ("Reopened", "Open"),
        ("Some Custom State", "Some Custom State"),
        ("", "Open"),
    ],
)
def test_status_mapping(raw: str, expected: str) -> None:
    assert canonical_status(raw) == expected


def test_resolution_mapping() -> None:
    assert canonical_resolution("Fixed") == "Done"
    assert canonical_resolution("Resolved") == "Done"
    assert canonical_resolution("Duplicate") == "Duplicate"
    assert canonical_resolution("Cannot Reproduce") == "Cannot Reproduce"
    assert canonical_resolution(None) is None


def test_datetime_parsing_accepts_jira_iso_and_native_values() -> None:
    assert parse_jira_datetime("2024-03-01T09:00:00.000+0000") == datetime(
        2024, 3, 1, 9, tzinfo=UTC
    )
    assert parse_jira_datetime("2024-03-01T09:00:00+10:00").utcoffset().total_seconds() == 36000
    assert parse_jira_datetime("2024-03-01T09:00:00Z") == datetime(2024, 3, 1, 9, tzinfo=UTC)
    naive = datetime(2024, 3, 1, 9)
    assert parse_jira_datetime(naive) == datetime(2024, 3, 1, 9, tzinfo=UTC)
    assert parse_jira_datetime(None) is None and parse_jira_datetime("garbage") is None


def test_text_of_flattens_adf() -> None:
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "a"}, {"type": "hardBreak"}],
            },
            {
                "type": "bulletList",
                "content": [{"type": "listItem", "content": [{"type": "text", "text": "b"}]}],
            },
        ],
    }
    assert text_of(doc) == "a\n\nb\n"
    assert text_of("plain") == "plain" and text_of(None) == ""


def test_export_keeps_one_project_redacts_and_writes_a_data_card(tmp_path: Path) -> None:
    out = tmp_path / "infra"
    stats = public_jira.export(FIXTURE, "INFRA", out)
    assert stats["raw_documents"] == 3 and stats["tickets"] == 3
    assert stats["by_status"] == {"Closed": 2, "Resolved": 1}
    assert stats["waited_on_requester"] == 1 and stats["with_code_changes"] == 1
    tickets = load_tickets(out)
    assert [t.id for t in tickets] == ["INFRA-1001", "INFRA-1002", "INFRA-1003"]
    first = tickets[0]
    assert "someone@example.org" not in first.description and "<email-1>" in first.description
    assert first.source == "public_jira"
    with gzip.open(out / "raw" / "INFRA.jsonl.gz", "rt", encoding="utf-8") as handle:
        raw = [json.loads(line) for line in handle if line.strip()]
    assert [r["key"] for r in raw] == ["INFRA-1001", "INFRA-1002", "INFRA-1003"]
    card = (out / "DATASET.md").read_text(encoding="utf-8")
    assert "Public Jira Dataset, project INFRA" in card and "unverified" in card
    assert "| Tickets | 3 |" in card and "CC BY 4.0" in card
    assert (
        (out / "platform.yaml")
        .read_text(encoding="utf-8")
        .strip()
        .endswith("procedure_changes: []")
    )


def test_bson_dump_files_are_read_without_a_database(tmp_path: Path) -> None:
    bson = pytest.importorskip("bson")
    dump = tmp_path / "Apache.bson"
    with dump.open("wb") as handle:
        for issue in _issues():
            issue = dict(issue)
            issue["fields"] = dict(issue["fields"])
            issue["fields"]["created"] = datetime(2024, 5, 1, tzinfo=UTC)  # Mongo stores dates
            handle.write(bson.encode(issue))
    keys = [i["key"] for i in public_jira.iter_issues(dump)]
    assert keys == ["INFRA-1001", "INFRA-1002", "INFRA-1003", "HADOOP-1"]
    stats = public_jira.export(dump, "HADOOP", tmp_path / "hadoop")
    assert stats["tickets"] == 1
    ticket = load_tickets(tmp_path / "hadoop")[0]
    assert ticket.created == datetime(2024, 5, 1, tzinfo=UTC)
    assert "dev@example.com" not in ticket.description and "203.0.113.7" not in ticket.description


def test_mongo_uri_needs_a_collection() -> None:
    with pytest.raises(ValueError):
        list(public_jira._iter_mongo("mongodb://localhost:27017/JiraRepos"))
