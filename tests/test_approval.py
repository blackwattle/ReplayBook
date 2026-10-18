"""Milestone 29: approval requests. The content of a request, "no live runs yet", the block
for an unreplayed version, a failed gate and an uncovered write, the live verification
rate, and the CLI and MCP doors. Offline."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from replaybook import packs
from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import ScriptedAgent
from replaybook.approval import (
    ApprovalError,
    PlannedWrite,
    build_request,
    parse_plan,
    render_markdown,
)
from replaybook.cli import app
from replaybook.eval import live
from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence
from replaybook.eval.labels import LabelChecks, record_check
from replaybook.eval.runner import run_replay
from replaybook.eval.testset import build_test_set
from replaybook.skills import skill_identity

runner = CliRunner()
PLAN = [
    {
        "tool": "kafka_create_topic",
        "args": {"cluster": "kafka-prod", "topic": "payments.events.v1", "partitions": 6},
    },
    {
        "tool": "kafka_add_acl",
        "args": {
            "cluster": "kafka-prod",
            "topic": "payments.events.v1",
            "principal": "svc-payments-prod",
            "operations": ["WRITE", "DESCRIBE"],
        },
    },
]


@dataclass
class Setup:
    root: Path
    runs: Path
    evidence: Path
    live: Path
    labels: Path
    checks: LabelChecks


@pytest.fixture()
def replayed(small_dataset: Path, tmp_path: Path) -> Setup:
    """A replay of the Kafka skill's current version, its label spot-check and its pack."""
    ts = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=12
    )
    runs = tmp_path / "runs"
    run = run_replay(ts, ScriptedAgent(), runs / "v2-kafka", workers=2)
    checks = LabelChecks()
    record_check(ts, checks, "ben")
    labels = tmp_path / "labels.yaml"
    checks.save(labels)
    build_evidence(
        EvidenceInputs(
            title="t",
            bundles=[RunBundle(run=run, test_set=ts, run_dir=runs / "v2-kafka")],
            label_checks=checks,
        ),
        tmp_path / "evidence" / "kafka",
    )
    return Setup(tmp_path, runs, tmp_path / "evidence", tmp_path / "live", labels, checks)


def _request(setup: Setup, plan: list[dict] | None = None, **kw: object) -> dict:
    return build_request(
        "kafka_topic",
        parse_plan(plan or PLAN),
        ticket="OPS-9001",
        runs_dir=setup.runs,
        evidence_dir=setup.evidence,
        live_dir=setup.live,
        label_checks=setup.checks,
        min_pass_rate=0.5,
        **kw,  # type: ignore[arg-type]
    )


def test_request_content(replayed: Setup) -> None:
    request = _request(replayed)
    version = skill_identity("kafka_topic")["skill_version"]
    assert not request["blocked"] and request["blocks"] == []
    assert request["skill"] == "kafka-topic-acls" and request["skill_version"] == version
    assert request["ticket"] == "OPS-9001"
    assert request["replay"]["result_line"].startswith("passed ")
    assert "past tickets (" in request["replay"]["result_line"]
    assert request["replay"]["go_live"]["verdict"] == "PASS"
    assert request["live_line"] == "no live runs yet"
    assert request["evidence_pack"] and request["evidence_pack"].endswith("evidence.html")
    create, acl = request["plan"]
    assert create["check"] == {
        "tool": "kafka_describe_topic",
        "args": {"cluster": "kafka-prod", "topic": "payments.events.v1"},
        "expect": {"partitions": 6},
        "timeout_s": 30.0,
    }, "an optional argument the write left out is not in the rendered check"
    assert acl["check"]["expect"]["acls"][0]["principal"] == "svc-payments-prod"
    text = render_markdown(request)
    for needle in (
        "# Approval request: kafka-topic-acls on OPS-9001",
        f"`{version[:12]}`",
        "passed ",
        "go-live gate PASS",
        "| Live verification | no live runs yet |",
        "evidence.html",
        "Check after the write: `kafka_describe_topic`",
        "Replaybook never approves",
    ):
        assert needle in text, needle
    assert "BLOCKED" not in text


def test_blocked_when_the_version_has_not_been_replayed(replayed: Setup, tmp_path: Path) -> None:
    copy = tmp_path / "packs"
    shutil.copytree(Path(packs.REPO_PACKS), copy, ignore=shutil.ignore_patterns("__pycache__"))
    skill = copy / "kafka" / "skills" / "kafka_topic_acls" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nOne more line.\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(packs.ENV_VAR, str(copy))
        packs.reset()
        try:
            request = _request(replayed)
        finally:
            packs.reset()
    assert request["blocked"] and request["replay"] is None
    assert request["blocks"] == [
        f"VERSION {request['skill_version_short']} OF THIS SKILL HAS NOT BEEN REPLAYED"
    ]
    text = render_markdown(request)
    assert text.splitlines()[2].startswith("> **BLOCKED: VERSION ")
    assert "HAS NOT BEEN REPLAYED. DO NOT APPROVE.**" in text
    assert "| Replay | none for this version |" in text


def test_blocked_when_the_replay_failed_the_gate(replayed: Setup) -> None:
    request = _request(replayed, plan=None)
    assert not request["blocked"]
    unchecked = build_request(
        "kafka_topic",
        parse_plan(PLAN),
        runs_dir=replayed.runs,
        evidence_dir=replayed.evidence,
        live_dir=replayed.live,
        label_checks=LabelChecks(),  # nobody spot-checked the labels
        min_pass_rate=0.5,
    )
    assert unchecked["blocked"]
    assert unchecked["blocks"][0].startswith("THE REPLAY OF VERSION")
    assert "LABELS NOT SPOT-CHECKED" in unchecked["blocks"][0]
    strict = build_request(
        "kafka_topic",
        parse_plan(PLAN),
        runs_dir=replayed.runs,
        evidence_dir=replayed.evidence,
        live_dir=replayed.live,
        label_checks=replayed.checks,
        min_pass_rate=1.0,
    )
    perfect = strict["replay"]["summary"]["pass_rate"] == 1.0
    assert strict["blocked"] is not perfect, "a 100% minimum blocks anything short of it"


def test_blocked_when_the_plan_holds_a_write_the_skill_does_not_cover(replayed: Setup) -> None:
    plan = [*PLAN, {"tool": "kafka_delete_topic", "args": {"cluster": "kafka-prod", "topic": "x"}}]
    request = _request(replayed, plan=plan)
    assert request["blocked"]
    assert request["blocks"] == [
        "THE PLAN INCLUDES WRITES THE SKILL DOES NOT COVER: kafka_delete_topic"
    ]
    assert request["plan"][2]["check"] is None
    assert "No check: the skill does not cover this write." in render_markdown(request)


def test_plans_and_skills_that_cannot_be_requested(replayed: Setup) -> None:
    for bad in ([], {"writes": []}, "x", [{"args": {}}], [{"tool": "ticket_comment", "args": {}}]):
        with pytest.raises(ApprovalError):
            parse_plan(bad)
    with pytest.raises(ApprovalError, match="has no skill"):
        build_request("dns_change", [PlannedWrite(tool="platform_dns_upsert_record")])


def test_live_verification_rate_and_degraded_warning(replayed: Setup) -> None:
    version = skill_identity("kafka_topic")["skill_version"] or ""
    for i, status in enumerate(["verified"] * 3 + ["verify_failed"]):
        report = live.LiveRunReport.model_validate(
            {
                "skill": "kafka_topic",
                "skill_version": version,
                "ticket": f"OPS-{i}",
                "at": datetime(2026, 10, 1, 9, i, tzinfo=UTC).isoformat(),
                "approval": {"decision": "approved", "by": "ben"},
                "writes": [{"tool": "kafka_create_topic", "args": {}, "verification": status}],
                "escalation": None
                if status == "verified"
                else {"reason": "partitions differ", "by": "verification"},
            }
        )
        live.record_run(report, replayed.live)
    request = _request(replayed)
    assert request["live_line"].startswith("75% of the last 4 live runs with writes verified")
    assert "4 of 4 runs on this version" in request["live_line"]
    assert request["warnings"] and "FLAGGED DEGRADED" in request["warnings"][0]
    assert not request["blocked"], "degraded is a warning for the approver, not a block"
    assert "**WARNING: THIS SKILL IS FLAGGED DEGRADED" in render_markdown(request)


def test_cli_approval_request_exit_codes(replayed: Setup, tmp_path: Path) -> None:
    common = [
        "--runs",
        str(replayed.runs),
        "--evidence",
        str(replayed.evidence),
        "--live-dir",
        str(replayed.live),
        "--labels-file",
        str(replayed.labels),
        "--min-pass-rate",
        "0.5",
    ]
    ok = runner.invoke(
        app,
        [
            "approval-request",
            "--skill",
            "kafka_topic",
            "--plan",
            json.dumps(PLAN),
            "--out",
            str(tmp_path / "req"),
            "--ticket",
            "OPS-9001",
            *common,
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert "ready for a person's decision" in ok.output
    assert (tmp_path / "req" / "approval-request.md").exists()
    data = json.loads((tmp_path / "req" / "approval-request.json").read_text(encoding="utf-8"))
    assert data["blocked"] is False and data["ticket"] == "OPS-9001"
    blocked = runner.invoke(
        app,
        [
            "approval-request",
            "--skill",
            "kafka_topic",
            "--plan",
            json.dumps([{"tool": "kafka_delete_topic", "args": {}}]),
            "--out",
            str(tmp_path / "req2"),
            *common,
        ],
    )
    assert blocked.exit_code == 1 and "BLOCKED" in blocked.output
    no_runs = runner.invoke(
        app,
        [
            "approval-request",
            "--skill",
            "kafka_topic",
            "--plan",
            json.dumps(PLAN),
            "--out",
            str(tmp_path / "req3"),
            "--runs",
            str(tmp_path / "nowhere"),
        ],
    )
    assert no_runs.exit_code == 1 and "HAS NOT BEEN REPLAYED" in no_runs.output
    bad = runner.invoke(
        app,
        [
            "approval-request",
            "--skill",
            "kafka_topic",
            "--plan",
            "[]",
            "--out",
            str(tmp_path / "x"),
        ],
    )
    assert bad.exit_code == 2 and not (tmp_path / "x").exists()
