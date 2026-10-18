"""Shadow mode: proposals on open tickets are stored, never executed, and scored once the
tickets are resolved; the evidence pack shows them in their own section."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import FLAWS, ScriptedAgent
from replaybook.cli import app
from replaybook.eval import shadow
from replaybook.eval.accepted import AcceptedDifferences
from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence, verify_manifest
from replaybook.eval.ground_truth import load_ground_truth
from replaybook.eval.runner import run_replay
from replaybook.eval.testset import build_test_set
from replaybook.schema.ticket import Ticket

OPEN_N = 40


def _as_open(ticket: Ticket) -> Ticket:
    """The ticket as it looked while still open: no resolution, no engineer work yet."""
    first_engineer = next((c.created for c in ticket.comments if c.role == "engineer"), None)
    comments = [c for c in ticket.comments if first_engineer is None or c.created < first_engineer]
    changes = [c for c in ticket.status_changes if c.to_status in ("Open", "In Progress")][:2]
    return ticket.model_copy(
        update={
            "status": "In Progress",
            "resolution": None,
            "resolved": None,
            "comments": comments,
            "status_changes": changes,
            "actions": [],
            "code_changes": [],
            "links": [],
            "time_spent_hours": None,
            "effort_source": None,
        }
    )


@pytest.fixture(scope="module")
def worlds(small_dataset: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """`now`: a dataset where the newest kafka tickets are still open. `later`: the real
    dataset, where they are resolved."""
    tickets = load_tickets(small_dataset, redact=False)
    truth = load_ground_truth(small_dataset)
    kafka = [t for t in tickets if truth[t.id].request_type == "kafka_topic" and t.resolved]
    newest = sorted(kafka, key=lambda t: t.created)[-OPEN_N:]
    open_ids = {t.id for t in newest}
    now_dir = tmp_path_factory.mktemp("now")
    with (now_dir / "tickets.jsonl").open("w", encoding="utf-8") as handle:
        for t in tickets:
            handle.write((_as_open(t) if t.id in open_ids else t).model_dump_json())
            handle.write("\n")
    (now_dir / "platform.yaml").write_text(
        (small_dataset / "platform.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {"now": now_dir, "later": small_dataset, "open_ids": open_ids}  # type: ignore[dict-item]


def test_record_then_score_catches_unsafe_proposals(
    worlds: dict[str, Path], tmp_path: Path
) -> None:
    now_dir, later_dir, open_ids = worlds["now"], worlds["later"], worlds["open_ids"]
    shadow_dir = tmp_path / "shadow"
    now_tickets = load_tickets(now_dir)
    platform = load_platform(now_dir)
    flawed = ScriptedAgent(flaws=FLAWS)

    stats = shadow.record(now_tickets, platform, "kafka_topic", flawed, shadow_dir)
    assert stats["open_tickets"] >= OPEN_N * 0.8, stats
    assert stats["recorded"] == stats["open_tickets"] and stats["already_recorded"] == 0
    proposals = shadow.load_proposals(shadow_dir)
    assert {p.ticket_id for p in proposals} <= open_ids | {
        t.id for t in now_tickets if shadow.is_open(t)
    }
    assert all(p.writes or p.stopped_reason == "asked" for p in proposals)
    assert any(w.tool == "kafka_delete_topic" for p in proposals for w in p.writes), (
        "the flawed agent proposed a destructive shortcut somewhere"
    )
    # Nothing left the mock: the open tickets in the dataset are untouched.
    assert all(shadow.is_open(t) for t in load_tickets(now_dir) if t.id in open_ids)

    again = shadow.record(now_tickets, platform, "kafka_topic", flawed, shadow_dir)
    assert again["recorded"] == 0 and again["already_recorded"] == stats["open_tickets"]
    fixed = shadow.record(now_tickets, platform, "kafka_topic", ScriptedAgent(), shadow_dir)
    assert fixed["recorded"] == stats["open_tickets"], "a different agent records its own proposals"

    # Scoring while the tickets are still open: everything pending, nothing scored.
    waiting = shadow.score(now_tickets, platform, shadow_dir)
    assert waiting["scored_now"] == 0 and waiting["pending"] == 2 * stats["open_tickets"]
    assert waiting["report"].scored == 0 and waiting["report"].pending == waiting["pending"]

    # Later, engineers resolved them: the proposals are scored against what they did.
    later_tickets = load_tickets(later_dir)
    scored = shadow.score(later_tickets, load_platform(later_dir), shadow_dir)
    # Every proposal is either scored or unscorable (a duplicate has no procedure to compare).
    assert scored["scored_now"] + scored["unscorable"] == 2 * stats["open_tickets"]
    assert scored["scored_now"] >= 2 * stats["open_tickets"] - 6
    report = scored["report"]
    assert report.scored == scored["scored_now"] and report.pending == 0
    assert report.unscorable == scored["unscorable"]
    assert report.unsafe_caught, "the destructive proposal was caught before anyone acted on it"
    assert all(
        u["severity"] == "critical" and "kafka_delete_topic" in u["tools"]
        for u in report.unsafe_caught
    )
    assert report.summary.total == report.scored and report.summary.critical_failures >= 1
    assert report.groups and sum(g.count for g in report.groups) == report.summary.failed
    assert len(report.agents) == 2
    scores = shadow.load_scores(shadow_dir)
    by_agent: dict[str, list[shadow.Score]] = {}
    for s in scores:
        by_agent.setdefault(s.fingerprint, []).append(s)
    flawed_fp = next(p.fingerprint for p in proposals)
    fixed_fp = next(fp for fp in by_agent if fp != flawed_fp)

    # On tickets raised after the procedure change the fixed agent does better; on the older
    # ones history itself is outdated (tagged pre-procedure-change) and the flawed agent
    # matches it by accident, exactly as in a replay.
    def passes(fp: str) -> int:
        return sum(
            s.result.outcome == "pass"
            for s in by_agent[fp]
            if "pre-procedure-change" not in s.result.tags
        )

    assert passes(fixed_fp) > passes(flawed_fp), (passes(fixed_fp), passes(flawed_fp))
    assert any("pre-procedure-change" in s.result.tags for s in scores)
    assert all(s.expectation_source == "history" and s.expectation_verified for s in scores)
    # Idempotent: scoring again adds nothing.
    assert shadow.score(later_tickets, load_platform(later_dir), shadow_dir)["scored_now"] == 0
    assert shadow.load_report(shadow_dir) is not None


def test_evidence_pack_gets_a_shadow_section(worlds: dict[str, Path], tmp_path: Path) -> None:
    now_dir, later_dir = worlds["now"], worlds["later"]
    shadow_dir = tmp_path / "shadow"
    shadow.record(
        load_tickets(now_dir),
        load_platform(now_dir),
        "kafka_topic",
        ScriptedAgent(flaws=["unsafe_write"]),
        shadow_dir,
    )
    shadow.score(load_tickets(later_dir), load_platform(later_dir), shadow_dir)
    report = shadow.load_report(shadow_dir)
    assert report is not None and report.scored > 0

    ts = build_test_set(load_tickets(later_dir), load_platform(later_dir), "artifactory_repo", n=8)
    run = run_replay(ts, ScriptedAgent(), tmp_path / "run", workers=2)
    paths = build_evidence(
        EvidenceInputs(
            title="With shadow",
            bundles=[RunBundle(run=run, test_set=ts, run_dir=tmp_path / "run")],
            accepted=AcceptedDifferences(),
            shadow=report,
            shadow_dir=shadow_dir,
        ),
        tmp_path / "evidence",
    )
    html = paths["html"].read_text(encoding="utf-8")
    assert "7. Shadow mode" in html and "Agent proposed (never executed)" in html
    assert "Unsafe proposals caught" in html
    assert "8. Sign-off" in html and "10. Manifest" in html
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["shadow"]["scored"] == report.scored and data["shadow"]["unsafe_caught"]
    assert any("Shadow results" in item for item in data["limitations"])
    roles = {f["role"] for f in json.loads(paths["manifest"].read_text(encoding="utf-8"))["inputs"]}
    assert {"shadow proposals.jsonl", "shadow scores.jsonl", "shadow shadow.json"} <= roles
    assert verify_manifest(tmp_path / "evidence") == []

    without = build_evidence(
        EvidenceInputs(title="No shadow", bundles=[RunBundle(run=run, test_set=ts)]),
        tmp_path / "evidence2",
    )
    assert "No shadow results were given" in without["html"].read_text(encoding="utf-8")


def test_cli_record_score_report_and_evidence(worlds: dict[str, Path], tmp_path: Path) -> None:
    now_dir, later_dir = worlds["now"], worlds["later"]
    runner = CliRunner()
    shadow_dir = tmp_path / "shadow"
    recorded = runner.invoke(
        app,
        [
            "shadow",
            "record",
            "--type",
            "kafka_topic",
            "--data",
            str(now_dir),
            "--out",
            str(shadow_dir),
            "--flaws",
            "guess_missing_info",
        ],
    )
    assert recorded.exit_code == 0, recorded.output
    assert "proposals recorded" in recorded.output and "Nothing was executed" in recorded.output
    pending = runner.invoke(
        app, ["shadow", "score", "--data", str(now_dir), "--out", str(shadow_dir)]
    )
    assert pending.exit_code == 0 and "scored 0 new" in pending.output, pending.output
    scored = runner.invoke(
        app, ["shadow", "score", "--data", str(later_dir), "--out", str(shadow_dir)]
    )
    assert scored.exit_code == 0, scored.output
    assert "0 pending" in scored.output and "pass rate" in scored.output
    shown = runner.invoke(app, ["shadow", "report", str(shadow_dir)])
    assert shown.exit_code == 0 and "kafka_topic" in shown.output
    ts_path = tmp_path / "ts.json"
    assert (
        runner.invoke(
            app,
            [
                "build-testset",
                "--type",
                "kafka_topic",
                "--data",
                str(later_dir),
                "--n",
                "6",
                "--out",
                str(ts_path),
            ],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "replay",
                "--testset",
                str(ts_path),
                "--out",
                str(tmp_path / "r"),
                "--accepted-file",
                str(tmp_path / "a.yaml"),
            ],
        ).exit_code
        == 0
    )
    evidence = runner.invoke(
        app,
        [
            "evidence",
            "--run",
            str(tmp_path / "r"),
            "--testset",
            str(ts_path),
            "--out",
            str(tmp_path / "ev"),
            "--shadow",
            str(shadow_dir),
            "--accepted-file",
            str(tmp_path / "a.yaml"),
            "--baseline-dir",
            str(tmp_path / "b"),
        ],
    )
    assert evidence.exit_code == 0, evidence.output
    assert "7. Shadow mode" in (tmp_path / "ev" / "evidence.html").read_text(encoding="utf-8")
    missing = runner.invoke(
        app,
        [
            "evidence",
            "--run",
            str(tmp_path / "r"),
            "--out",
            str(tmp_path / "ev2"),
            "--shadow",
            str(tmp_path / "nowhere"),
        ],
    )
    assert missing.exit_code == 2 and "no shadow.json" in missing.output
