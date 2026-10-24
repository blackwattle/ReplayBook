from __future__ import annotations

import json
import time
from pathlib import Path

from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import ScriptedAgent
from replaybook.cli import app
from replaybook.demo import run_demo
from replaybook.eval.accepted import AcceptedDifferences
from replaybook.eval.evidence import (
    EvidenceInputs,
    RunBundle,
    build_evidence,
    sha256_file,
    verify_manifest,
)
from replaybook.eval.runner import run_replay
from replaybook.eval.testset import build_test_set


def test_evidence_pack_is_complete_and_hashed(small_dataset: Path, tmp_path: Path) -> None:
    ts = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=20
    )
    ts_path = tmp_path / "ts.json"
    ts_path.write_text(ts.model_dump_json(), encoding="utf-8")
    run_dir = tmp_path / "run"
    run = run_replay(
        ts, ScriptedAgent(flaws=["unsafe_write", "guess_missing_info"]), run_dir, workers=2
    )
    store = AcceptedDifferences()
    failing = [r for r in run.results if r.outcome in ("mismatch", "partial")]
    store.add("case", failing[0].case_id, "reviewed", "reviewer")
    acc_path = tmp_path / "accepted.yaml"
    store.save(acc_path)

    out = tmp_path / "evidence"
    paths = build_evidence(
        EvidenceInputs(
            title="Test pack",
            bundles=[RunBundle(run=run, test_set=ts, run_dir=run_dir, test_set_path=ts_path)],
            accepted=store,
            accepted_path=acc_path,
            data_dir=small_dataset,
            quality={"false passes": 0},
        ),
        out,
    )
    html = paths["html"].read_text(encoding="utf-8")
    for needle in (
        "Scope and fingerprints",
        "Test-set source",
        "Outcomes and the go-live gate",
        "Go-live gate: FAIL",
        "passed ",
        "Wilson",
        "Failure groups",
        "Engineers did (history)",
        "Agent did (recorded writes)",
        "Accepted differences",
        "Changes since the baseline",
        "Sign-off",
        "Limitations",
        "Manifest",
        run.fingerprint,
        "kafka_topic",
        "reviewed",
        "local mock",
    ):
        assert needle in html, needle
    assert "<script" not in html
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["gate"] == "FAIL" and data["pass_rates"][0]["critical_failures"] >= 1
    assert data["failure_groups"][0]["groups"], "groups must be in the JSON"
    assert all(len(g["examples"]) <= 2 for g in data["failure_groups"][0]["groups"])
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    roles = {f["role"] for f in manifest["inputs"]}
    assert (
        "accepted differences" in roles
        and "data tickets.jsonl" in roles
        and f"test set {ts.name}" in roles
    )
    assert {o["path"] for o in manifest["outputs"]} == {"evidence.html", "evidence.json"}
    assert verify_manifest(out) == []
    paths["html"].write_text(html + "\n<!-- tampered -->", encoding="utf-8")
    assert verify_manifest(out) == ["evidence.html"]
    assert sha256_file(paths["json"]) == next(
        o["sha256"] for o in manifest["outputs"] if o["path"] == "evidence.json"
    )


def test_demo_runs_end_to_end_offline(tmp_path: Path) -> None:
    lines: list[str] = []
    started = time.perf_counter()
    # Seed 10 puts tickets that needed a person's judgement among the newest of every type.
    outcome = run_demo(
        tmp_path / "demo", seed=10, n_tickets=700, cases=15, workers=3, say=lines.append
    )
    elapsed = time.perf_counter() - started
    text = "\n".join(lines)
    assert elapsed < 120, elapsed
    for step in (
        "1. Generated",
        "2. Built test sets",
        "3. Replayed",
        "4. Grouped",
        "5. Marked",
        "6. Approved",
        "7. Wrote",
    ):
        assert step in text, step
    assert (
        "Guessed missing information" in text and "Wrong tool" in text and "Unsafe action" in text
    )
    assert "Missed escalation" in text and "Missed escalation, e.g." in text
    assert "Correct escalation, e.g." in text and "handed back:" in text
    assert "correctly escalated" in text and "go-live gate PASS" in text
    assert "8. Wrote an approval request" in text and "ready for a person's decision" in text
    request = Path(outcome["approval_request"]["markdown"]).read_text(encoding="utf-8")
    assert "BLOCKED" not in request and "Check after the write" in request
    assert "no live runs yet" in request
    assert all(v.startswith("0 of") for k, v in outcome["quality"].items() if "disagree" in k)
    assert outcome["exit_codes"] == [0, 0, 0], text
    assert all(v == 0 for k, v in outcome["quality"].items() if "false passes" in k), outcome[
        "quality"
    ]
    assert all(float(v) >= 0.9 for k, v in outcome["quality"].items() if "purity" in k), outcome[
        "quality"
    ]
    assert outcome["accepted"] >= 1
    evidence = tmp_path / "demo" / "evidence"
    assert (evidence / "evidence.html").exists() and (evidence / "evidence.json").exists()
    assert verify_manifest(evidence) == []
    assert (tmp_path / "demo" / "assurance" / "accepted.yaml").exists()
    assert (tmp_path / "demo" / "assurance" / "labels.yaml").exists()
    assert (tmp_path / "demo" / "assurance" / "baselines" / "kafka_topic.json").exists()
    data = json.loads((evidence / "evidence.json").read_text(encoding="utf-8"))
    assert data["gate"] == "PASS" and data["critical_gate"] == "PASS"
    assert all(g["verdict"] == "PASS" for g in data["go_live"])
    assert all(p["result_line"].startswith("passed ") for p in data["pass_rates"])
    assert sum(p["outcomes"]["correct_escalation"] for p in data["pass_rates"]) >= 1
    assert all(s["skill_version"] and len(s["skill_version_short"]) == 12 for s in data["scope"])
    html = (evidence / "evidence.html").read_text(encoding="utf-8")
    assert "Go-live gate: PASS" in html and "Correctly escalated" in html
    assert "automated stand-in" in html, "the pack says the label check was not a person's"
    assert {p["request_type"] for p in data["pass_rates"]} == {
        "kafka_topic",
        "artifactory_repo",
        "openstack_quota",
    }
    assert all(p["critical_failures"] == 0 for p in data["pass_rates"])


def test_demo_cli_and_evidence_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["demo", "--out", str(tmp_path / "d"), "--n", "500", "--cases", "10", "--seed", "3"]
    )
    assert result.exit_code == 0, result.output
    assert "Done in" in result.output
    run_dirs = sorted((tmp_path / "d" / "runs").glob("v2-*"))
    assert len(run_dirs) == 3
    result = runner.invoke(
        app,
        [
            "evidence",
            "--run",
            str(run_dirs[0]),
            "--run",
            str(run_dirs[1]),
            "--out",
            str(tmp_path / "ev"),
            "--baseline-dir",
            str(tmp_path / "d" / "assurance" / "baselines"),
            "--accepted-file",
            str(tmp_path / "d" / "assurance" / "accepted.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert verify_manifest(tmp_path / "ev") == []
