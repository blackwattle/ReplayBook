"""One collection and one project can be pulled out of a mongodump archive without a
database, and other collections are skipped without being decoded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from replaybook.adapters import public_jira
from replaybook.adapters.mongo_archive import (
    ArchiveFormatError,
    issue_key,
    iter_collection,
    list_collections,
    write_archive,
)
from replaybook.adapters.synthetic import load_tickets
from replaybook.cli import app

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "jira" / "issues.jsonl"


def _issues() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    pytest.importorskip("bson")
    issues = _issues()
    others = [
        {"_id": i, "key": f"QTBUG-{i}", "fields": {"project": {"key": "QTBUG"}}} for i in range(3)
    ]
    path = tmp_path / "mongodump-JiraReposAnon.archive"
    write_archive(path, {"Qt": others, "Apache": issues}, db="JiraReposAnon")
    return path


def test_list_collections_reads_only_the_prelude(archive: Path) -> None:
    found = list_collections(archive)
    assert [(c["db"], c["collection"]) for c in found] == [
        ("JiraReposAnon", "Qt"),
        ("JiraReposAnon", "Apache"),
    ]


def test_iter_collection_filters_by_collection_and_key_prefix(archive: Path) -> None:
    keys = [d["key"] for d in iter_collection(archive, "Apache")]
    assert keys == ["INFRA-1001", "INFRA-1002", "INFRA-1003", "HADOOP-1"]
    infra = [d["key"] for d in iter_collection(archive, "Apache", key_prefix="INFRA-")]
    assert infra == ["INFRA-1001", "INFRA-1002", "INFRA-1003"]
    assert [d["key"] for d in iter_collection(archive, "Qt")] == ["QTBUG-0", "QTBUG-1", "QTBUG-2"]
    assert list(iter_collection(archive, "Nope")) == []
    assert list(iter_collection(archive, "Apache", db="other")) == []


def test_issue_key_is_read_from_raw_bytes() -> None:
    bson = pytest.importorskip("bson")
    raw = bson.encode({"_id": 1, "id": "5", "key": "INFRA-42", "fields": {"x": "y" * 3000}})
    assert issue_key(raw) == "INFRA-42"
    assert issue_key(bson.encode({"fields": {"key": "nested"}})) is None


def test_bad_magic_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "x.archive"
    bad.write_bytes(b"not an archive at all")
    with pytest.raises(ArchiveFormatError):
        list_collections(bad)


def test_export_from_archive_and_cli(archive: Path, tmp_path: Path) -> None:
    stats = public_jira.export(archive, "INFRA", tmp_path / "infra", collection="Apache")
    assert stats["raw_documents"] == 3 and stats["tickets"] == 3
    assert [t.id for t in load_tickets(tmp_path / "infra")] == [
        "INFRA-1001",
        "INFRA-1002",
        "INFRA-1003",
    ]
    with pytest.raises(ValueError, match="collection"):
        public_jira.export(archive, "INFRA", tmp_path / "x")
    runner_result = (
        __import__("typer.testing", fromlist=["CliRunner"])
        .CliRunner()
        .invoke(app, ["public-jira", "collections", "--input", str(archive)])
    )
    assert runner_result.exit_code == 0 and "JiraReposAnon.Apache" in runner_result.output
