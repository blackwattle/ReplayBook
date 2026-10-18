"""`replaybook sync`: newly resolved tickets join a test set; existing cases do not move."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import ScriptedAgent
from replaybook.cli import app
from replaybook.eval.baseline import approve, check
from replaybook.eval.runner import run_replay
from replaybook.eval.sync import sync_test_set, synced_through
from replaybook.eval.testset import build_test_set
from replaybook.schema.ticket import Ticket


def _slice(data_dir: Path) -> tuple[list[Ticket], list[Ticket], Path]:
    """An early 70% of the history (by creation) and the full history, plus a directory
    holding only the early slice so a set can be built from it."""
    tickets = load_tickets(data_dir)
    cut = int(len(tickets) * 0.7)
    early = tickets[:cut]
    early_dir = data_dir.parent / "early"
    early_dir.mkdir(exist_ok=True)
    with (early_dir / "tickets.jsonl").open("w", encoding="utf-8") as handle:
        for t in load_tickets(data_dir, redact=False)[:cut]:
            handle.write(t.model_dump_json())
            handle.write("\n")
    (early_dir / "platform.yaml").write_text(
        (data_dir / "platform.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return early, tickets, early_dir


def test_sync_adds_only_newer_resolved_tickets_and_is_idempotent(small_dataset: Path) -> None:
    early, full, early_dir = _slice(small_dataset)
    platform = load_platform(small_dataset)
    original = build_test_set(early, platform, "kafka_topic", n=15, data_dir=str(early_dir))
    before = {c.id: c.model_dump(mode="json") for c in original.cases}
    through = synced_through(original)
    assert through is not None

    updated, entry = sync_test_set(original, full, platform, data_dir=str(small_dataset))
    assert entry["added"], "the newer 30% of history holds resolved kafka tickets"
    assert set(entry["added"]).isdisjoint(before)
    by_id = {t.id: t for t in full}
    for case_id in entry["added"]:
        ticket = by_id[case_id]
        assert ticket.resolved is not None and ticket.resolved > through
        assert ticket.actions or updated.case(case_id).expectation.kind == "ask"
    for case_id, dumped in before.items():
        assert updated.case(case_id).model_dump(mode="json") == dumped, "old cases untouched"
    assert len(updated.cases) == len(before) + len(entry["added"])
    assert [c.id for c in updated.cases] == sorted(c.id for c in updated.cases)
    assert updated.selection["sync"][-1] == entry and updated.selection["synced_through"]
    assert updated.selection["selected"] == len(updated.cases)
    assert updated.sha256() != original.sha256()
    # Every newer resolved kafka ticket was either added or skipped for a stated reason.
    assert entry["candidates"] == len(entry["added"]) + sum(entry["skipped"].values())
    assert all(reason for reason in entry["skipped"])

    again, second = sync_test_set(updated, full, platform, data_dir=str(small_dataset))
    assert second["added"] == [] and second["candidates"] == 0
    assert [c.id for c in again.cases] == [c.id for c in updated.cases]
    assert len(again.selection["sync"]) == 2

    capped, third = sync_test_set(original, full, platform, max_new=2, data_dir=str(small_dataset))
    assert len(third["added"]) == 2 and third["skipped"]["beyond --max"] == len(entry["added"]) - 2
    assert third["added"] == entry["added"][:2], "oldest resolutions first"


def test_check_reports_synced_cases_as_new(small_dataset: Path, tmp_path: Path) -> None:
    early, full, early_dir = _slice(small_dataset)
    platform = load_platform(small_dataset)
    original = build_test_set(early, platform, "openstack_quota", n=12, data_dir=str(early_dir))
    baseline_dir = tmp_path / "baselines"
    approve(
        run_replay(original, ScriptedAgent(), tmp_path / "base", workers=2), "ben", baseline_dir
    )
    updated, entry = sync_test_set(original, full, platform, data_dir=str(small_dataset))
    assert entry["added"]
    report, run = check(updated, ScriptedAgent(), tmp_path / "check", baseline_dir=baseline_dir)
    assert report.fingerprint_changed and report.reran and run is not None
    assert set(report.new_cases) == set(entry["added"])
    assert not report.regressions, "old cases did not move, so nothing regresses"


def test_cli_sync_rewrites_the_file(small_dataset: Path, tmp_path: Path) -> None:
    early, _full, early_dir = _slice(small_dataset)
    original = build_test_set(
        early, load_platform(small_dataset), "artifactory_repo", n=10, data_dir=str(early_dir)
    )
    path = tmp_path / "artifactory_repo.json"
    path.write_text(original.model_dump_json(indent=2), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["sync", "--testset", str(path), "--data", str(small_dataset), "--max", "3"]
    )
    assert result.exit_code == 0, result.output
    assert "3 new cases" in result.output and result.output.count("  NEW ") == 3
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["cases"]) == len(original.cases) + 3
    assert data["selection"]["sync"][0]["skipped"].get("beyond --max", 0) >= 0
    missing = CliRunner().invoke(
        app, ["sync", "--testset", str(path), "--data", str(tmp_path / "nowhere")]
    )
    assert missing.exit_code == 2 and "no tickets.jsonl" in missing.output
