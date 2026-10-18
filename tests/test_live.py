"""Milestone 28: scoring after go-live. The live-run record and its refusals, hours saved
only for verified runs, rolling windows, thresholds, the degraded flag and its
transitions, `live check`, `status` and the evidence pack's live section. Offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import ScriptedAgent
from replaybook.cli import app
from replaybook.eval import live
from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence
from replaybook.eval.runner import run_replay
from replaybook.eval.testset import build_test_set
from replaybook.skills import skill_identity

runner = CliRunner()
START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
VERSION = skill_identity("kafka_topic")["skill_version"] or ""
WRITE = {"tool": "kafka_create_topic", "args": {"topic": "a.events.v1"}}


def _report(
    i: int,
    decision: str = "approved",
    verification: str | None = "verified",
    escalation: dict[str, str] | None = None,
    skill: str = "kafka_topic",
    version: str = VERSION[:12],
) -> live.LiveRunReport:
    writes = [] if verification is None else [{**WRITE, "verification": verification}]
    if escalation is None and verification not in (None, "verified"):
        escalation = {"reason": "the check failed", "by": "verification"}
    return live.LiveRunReport.model_validate(
        {
            "skill": skill,
            "skill_version": version,
            "ticket": f"OPS-{i}",
            "at": (START + timedelta(hours=i)).isoformat(),
            "approval": {"decision": decision, "by": "ben"},
            "writes": writes if decision == "approved" else [],
            "escalation": escalation,
        }
    )


@pytest.fixture()
def discovery(tmp_path: Path) -> Path:
    path = tmp_path / "discovery.json"
    path.write_text(
        json.dumps({"types": [{"key": "kafka_topic", "median_hours": 1.5, "tickets": 40}]}),
        encoding="utf-8",
    )
    return path


def test_record_computes_verification_and_hours(tmp_path: Path, discovery: Path) -> None:
    directory = tmp_path / "live"
    run = live.record_run(_report(1), directory, discovery)
    assert run.id == "LIVE-1" and run.verification == "verified" and not run.escalated
    assert run.skill_version == VERSION, "a 12-character prefix of the current version is expanded"
    assert run.skill_name == "kafka-topic-acls"
    assert run.hours_saved == 1.5 and "median of 40 past tickets" in run.hours_basis
    failed = live.record_run(_report(2, verification="verify_failed"), directory, discovery)
    assert failed.verification == "verify_failed" and failed.escalated and failed.hours_saved == 0.0
    rejected = live.record_run(_report(3, decision="rejected"), directory, discovery)
    assert rejected.verification == "none" and rejected.hours_saved == 0.0
    handed_back = live.record_run(
        _report(4, verification=None, escalation={"reason": "needs CAB", "by": "skill"}),
        directory,
        discovery,
    )
    assert handed_back.verification == "none" and handed_back.hours_saved == 0.0
    unknown = live.record_run(_report(5), directory, tmp_path / "missing.json")
    assert unknown.hours_saved is None and "no discovery report" in unknown.hours_basis
    lines = (directory / live.RUNS_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == [f"LIVE-{i}" for i in range(1, 6)]


def test_records_that_break_the_contract_are_refused(tmp_path: Path) -> None:
    directory = tmp_path / "live"
    bad: list[dict[str, Any]] = [
        # a rejected run that made writes
        {
            "approval": {"decision": "rejected", "by": "ben"},
            "writes": [{**WRITE, "verification": "verified"}],
        },
        # a failed check with no hand-back
        {"writes": [{**WRITE, "verification": "verify_error"}]},
        # nobody approved
        {"approval": {"decision": "approved", "by": "  "}},
        # a request type no pack defines
        {"skill": "widget_order"},
    ]
    base = _report(1).model_dump(mode="json")
    for change in bad:
        with pytest.raises(ValueError):
            live.record_run(live.LiveRunReport.model_validate(base | change), directory)
    for version in ("abc", "not-hex-at-all!"):
        with pytest.raises(ValueError):
            live.LiveRunReport.model_validate(base | {"skill_version": version})
    with pytest.raises(ValueError):
        live.LiveRunReport.model_validate(base | {"surprise": 1})
    assert not (directory / live.RUNS_FILE).exists(), "nothing refused was written"


def test_rolling_window_and_thresholds(
    tmp_path: Path, discovery: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "live"
    # 10 old runs that all failed their check, then 30 verified ones.
    for i in range(10):
        live.record_run(_report(i, verification="verify_failed"), directory, discovery)
    for i in range(10, 40):
        live.record_run(_report(i), directory, discovery)
    report = live.build_report(directory)
    [skill] = report.skills
    assert skill.window == 30 and skill.runs_total == 40 and skill.runs_in_window == 30
    assert skill.verification_rate == 1.0 and not skill.degraded, "old failures left the window"
    assert skill.hours_saved_window == 45.0 and skill.hours_saved_total == 45.0
    wide = live.build_report(directory, window_runs=40).skills[0]
    assert wide.verification_rate == 0.75 and wide.degraded
    assert wide.reasons == ["verification rate 75% is below 95%"]
    monkeypatch.setenv(live.ENV_WINDOW, "40")
    assert live.build_report(directory).skills[0].degraded
    monkeypatch.setenv(live.ENV_MIN_VERIFICATION, "0.7")
    assert not live.build_report(directory).skills[0].degraded, "the threshold is configurable"
    monkeypatch.setenv(live.ENV_WINDOW, "zero")
    with pytest.raises(ValueError):
        live.build_report(directory)


def test_approval_rate_and_escalation_rate(tmp_path: Path) -> None:
    directory = tmp_path / "live"
    for i in range(7):
        live.record_run(_report(i), directory)
    for i in range(7, 10):
        live.record_run(_report(i, decision="rejected"), directory)
    [skill] = live.build_report(directory).skills
    assert skill.approval_rate == 0.7 and skill.degraded
    assert skill.reasons == ["approval rate 70% is below 80%"]
    assert skill.escalation_rate == 0.0 and skill.verification_rate == 1.0
    assert skill.rejected == 3 and skill.with_writes == 7
    assert "DEGRADED" in skill.line()


def test_degraded_transitions_are_written_once(tmp_path: Path) -> None:
    directory = tmp_path / "live"
    for i in range(3):
        live.record_run(_report(i), directory)
    assert live.load_transitions(directory) == [], "healthy from the start: nothing to say"
    live.record_run(_report(3, verification="verify_failed"), directory)
    live.record_run(_report(4, verification="verify_failed"), directory)
    lines = live.load_transitions(directory)
    assert [x["state"] for x in lines] == ["degraded"], "a skill that stays degraded adds nothing"
    assert lines[0]["skill"] == "kafka_topic" and "verification rate" in lines[0]["reasons"][0]
    for i in range(5, 40):
        live.record_run(_report(i), directory)
    assert [x["state"] for x in live.load_transitions(directory)] == ["degraded", "recovered"]


def test_cli_live_record_report_check_and_status(tmp_path: Path, discovery: Path) -> None:
    directory = tmp_path / "live"
    (tmp_path / "run.json").write_text(_report(1).model_dump_json(), encoding="utf-8")
    recorded = runner.invoke(
        app,
        [
            "live",
            "record",
            "--run",
            str(tmp_path / "run.json"),
            "--live-dir",
            str(directory),
            "--discovery",
            str(discovery),
        ],
    )
    assert recorded.exit_code == 0, recorded.output
    assert "LIVE-1: kafka_topic on OPS-1, approved by ben, verification verified" in recorded.output
    refused = runner.invoke(
        app,
        ["live", "record", "--run", '{"skill": "kafka_topic"}', "--live-dir", str(directory)],
    )
    assert refused.exit_code == 2 and "not recorded" in refused.output
    report = runner.invoke(app, ["live", "report", "--live-dir", str(directory)])
    assert report.exit_code == 0 and "approval 100%" in report.output
    healthy = runner.invoke(app, ["live", "check", "--live-dir", str(directory)])
    assert healthy.exit_code == 0
    failing = _report(2, verification="verify_failed").model_dump_json()
    runner.invoke(app, ["live", "record", "--run", failing, "--live-dir", str(directory)])
    degraded = runner.invoke(app, ["live", "check", "--live-dir", str(directory)])
    assert degraded.exit_code == 1 and "DEGRADED" in degraded.output
    quiet = {
        live.ENV_LIVE_DIR: str(directory),
        "REPLAYBOOK_OFFLINE": "true",  # no model probe
        "REPLAYBOOK_CACHE_DIR": str(tmp_path / "cache"),
    }
    status = runner.invoke(app, ["status"], env=quiet)
    assert status.exit_code == 0 and "live runs: 1 skill(s)" in status.output
    assert "DEGRADED: kafka_topic" in status.output
    empty = runner.invoke(app, ["live", "report", "--live-dir", str(tmp_path / "none")])
    assert empty.exit_code == 0 and "no live runs recorded" in empty.output


def test_evidence_pack_live_section(small_dataset: Path, tmp_path: Path) -> None:
    ts = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=6
    )
    run = run_replay(ts, ScriptedAgent(), tmp_path / "run", workers=2)
    directory = tmp_path / "live"
    for i in range(4):
        live.record_run(_report(i), directory)
    paths = build_evidence(
        EvidenceInputs(
            title="t",
            bundles=[RunBundle(run=run, test_set=ts)],
            live=live.build_report(directory),
            live_dir=directory,
        ),
        tmp_path / "ev",
    )
    html = paths["html"].read_text(encoding="utf-8")
    assert "9. Live runs after go-live" in html and "4 of 4" in html
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["live"]["skills"][0]["approval_rate"] == 1.0
    roles = {f["role"] for f in json.loads(paths["manifest"].read_text(encoding="utf-8"))["inputs"]}
    assert "live runs.jsonl" in roles
    without = build_evidence(
        EvidenceInputs(title="t", bundles=[RunBundle(run=run, test_set=ts)]), tmp_path / "ev2"
    )
    assert "No live records were given" in without["html"].read_text(encoding="utf-8")
