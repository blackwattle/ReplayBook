from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import FLAWS, ScriptedAgent
from replaybook.cli import app
from replaybook.eval.accepted import AcceptedDifferences, apply_accepted, suggest
from replaybook.eval.groups import group_failures
from replaybook.eval.quality import group_purity
from replaybook.eval.runner import RunResult, run_replay
from replaybook.eval.testset import build_test_set

TODAY = date(2026, 10, 18)


@pytest.fixture(scope="module")
def flawed_kafka(small_dataset: Path, tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    ts = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=40
    )
    return run_replay(ts, ScriptedAgent(flaws=FLAWS), tmp_path_factory.mktemp("flawed"), workers=3)


@pytest.fixture(scope="module")
def fixed_quota(small_dataset: Path, tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    ts = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "openstack_quota", n=40
    )
    return run_replay(ts, ScriptedAgent(), tmp_path_factory.mktemp("fixed"), workers=3)


def test_groups_match_seeded_flaws(flawed_kafka: RunResult, small_dataset: Path) -> None:
    groups = group_failures(flawed_kafka.results)
    assert groups
    assert sum(g.count for g in groups) == flawed_kafka.summary.failed
    by_reason = {g.reason for g in groups}
    assert {"guessed_missing_info", "wrong_tool", "unsafe_action"} <= by_reason
    for g in groups:
        assert g.label and g.cause and g.id.startswith("G")
        assert len(g.examples) == min(2, g.count)
        assert all(ex.expected or ex.recorded for ex in g.examples)
        members = [r for r in flawed_kafka.results if r.case_id in g.case_ids]
        assert {m.reason for m in members} == {g.reason}
    wrong_tool = next(g for g in groups if g.reason == "wrong_tool")
    assert "superseded" in wrong_tool.cause and "kafka_zk" in wrong_tool.label
    guessed = next(g for g in groups if g.reason == "guessed_missing_info")
    assert "ask" in guessed.cause
    unsafe = next(g for g in groups if g.reason == "unsafe_action")
    assert unsafe.severity == "critical" and "kafka_delete_topic" in unsafe.label
    assert group_purity(groups, flawed_kafka, small_dataset) >= 0.9
    # Deterministic: same input, same groups.
    assert [g.model_dump() for g in group_failures(flawed_kafka.results)] == [
        g.model_dump() for g in groups
    ]


def test_fixed_run_failures_are_flagged_as_outdated_history(fixed_quota: RunResult) -> None:
    groups = group_failures(fixed_quota.results)
    assert groups, "expected pre-procedure-change tickets in the newest quota tickets"
    assert all(g.likely_history_outdated for g in groups)
    assert all("predates a procedure change" in g.cause for g in groups if g.reason == "wrong_tool")
    suggestions = suggest(fixed_quota.results)
    assert {s["case_id"] for s in suggestions} == {
        r.case_id for r in fixed_quota.results if r.outcome == "fail"
    }
    assert all(
        s["kind"] == "outdated_history" for s in suggestions if "pre-procedure-change" in s["tags"]
    )


def test_accept_by_case_and_signature_with_expiry(fixed_quota: RunResult, tmp_path: Path) -> None:
    failing = [r for r in fixed_quota.results if r.outcome == "fail"]
    assert len(failing) >= 2
    store = AcceptedDifferences()
    entry = store.add(
        "case",
        failing[0].case_id,
        "History predates the unified quota tool.",
        "ben",
        days=30,
        kind="outdated_history",
        today=TODAY,
    )
    assert entry.id == "ACC-1" and entry.expires == date(2026, 11, 17)
    applied = apply_accepted(fixed_quota.results, store, today=TODAY)
    first = next(r for r in applied if r.case_id == failing[0].case_id)
    assert first.outcome == "accepted" and "ben" in (first.accepted_by or "")
    assert sum(r.outcome == "accepted" for r in applied) == 1

    later = apply_accepted(fixed_quota.results, store, today=date(2026, 12, 1))
    expired = next(r for r in later if r.case_id == failing[0].case_id)
    assert expired.outcome == "fail" and "expired" in (expired.accepted_reason or "")

    groups = group_failures(fixed_quota.results)
    store.add(
        "signature", groups[0].signature, "Whole group: pre-change history.", "ben", today=TODAY
    )
    by_group = apply_accepted(fixed_quota.results, store, today=TODAY)
    accepted_ids = {r.case_id for r in by_group if r.outcome == "accepted"}
    assert set(groups[0].case_ids) <= accepted_ids

    path = tmp_path / "accepted.yaml"
    store.save(path)
    loaded = AcceptedDifferences.load(path)
    assert loaded == store
    with pytest.raises(ValueError):
        store.add("case", "X", "", "ben", today=TODAY)
    with pytest.raises(ValueError):
        store.add("case", "X", "why", "", today=TODAY)


def test_cli_groups_and_accept(small_dataset: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    ts_path = tmp_path / "ts.json"
    accepted_file = tmp_path / "accepted.yaml"
    run_dir = tmp_path / "run"
    assert (
        runner.invoke(
            app,
            [
                "build-testset",
                "--type",
                "openstack_quota",
                "--data",
                str(small_dataset),
                "--n",
                "40",
                "--out",
                str(ts_path),
            ],
        ).exit_code
        == 0
    )
    result = runner.invoke(
        app,
        [
            "replay",
            "--testset",
            str(ts_path),
            "--out",
            str(run_dir),
            "--accepted-file",
            str(accepted_file),
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["groups", str(run_dir)])
    assert result.exit_code == 0 and "G1" in result.output and (run_dir / "groups.json").exists()
    result = runner.invoke(app, ["accept", "--suggest", "--run", str(run_dir)])
    assert result.exit_code == 0 and "outdated_history" in result.output
    result = runner.invoke(
        app,
        [
            "accept",
            "--group",
            "G1",
            "--run",
            str(run_dir),
            "--reason",
            "pre-change history",
            "--approver",
            "ben",
            "--kind",
            "outdated_history",
            "--expires",
            "2027-01-01",
            "--accepted-file",
            str(accepted_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Recorded ACC-1" in result.output and "accepted" in result.output
    assert accepted_file.exists()
    report = runner.invoke(app, ["report", str(run_dir)])
    assert "accepted" in report.output
    bad = runner.invoke(
        app,
        ["accept", "--case", "OPS-1", "--approver", "ben", "--accepted-file", str(accepted_file)],
    )
    assert bad.exit_code != 0
