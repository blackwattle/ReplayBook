from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import ScriptedAgent
from replaybook.cli import app
from replaybook.eval.accepted import AcceptedDifferences
from replaybook.eval.baseline import GOOD, approve, check, load_baseline
from replaybook.eval.runner import compute_fingerprint, run_replay
from replaybook.eval.scoring import ACCEPTABLE, FAILING
from replaybook.eval.testset import build_test_set
from replaybook.schema.testcase import TestSet as Suite


@pytest.fixture(scope="module")
def suite(small_dataset: Path) -> Suite:
    return build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=30
    )


class _Crashing:
    name = "crashing"
    config = {"agent": "crashing", "version": 1}

    def run(self, env):
        raise RuntimeError("boom")


def test_approve_then_check_only_reports_differences(suite: Suite, tmp_path: Path) -> None:
    baseline_dir = tmp_path / "baselines"
    fixed = ScriptedAgent()
    baseline_run = run_replay(suite, fixed, tmp_path / "base", workers=3)
    baseline = approve(baseline_run, "reviewer", baseline_dir)
    assert load_baseline(suite.name, baseline_dir) == baseline
    assert baseline.fingerprint == baseline_run.fingerprint

    # Same agent, same test set, same tools: nothing changed, nothing re-run.
    report, run = check(suite, ScriptedAgent(), tmp_path / "check-same", baseline_dir=baseline_dir)
    assert not report.fingerprint_changed and not report.reran and run is None
    assert report.exit_code == 0

    # Forced re-run of an identical agent shows no differences.
    report, run = check(
        suite, ScriptedAgent(), tmp_path / "check-force", baseline_dir=baseline_dir, force=True
    )
    assert report.reran and run is not None and not report.has_differences
    assert report.exit_code == 0

    # A changed agent config changes the fingerprint and the flaws show up as regressions only.
    flawed = ScriptedAgent(flaws=["superseded_procedure", "unsafe_write"])
    report, run = check(suite, flawed, tmp_path / "check-flawed", baseline_dir=baseline_dir)
    assert report.fingerprint_changed and report.reran and run is not None
    assert "agent config: flaws" in report.message
    assert report.regressions, "flaws must regress cases that passed in the baseline"
    passed_before = {cid for cid, o in baseline.outcomes.items() if o["outcome"] in GOOD}
    assert all(c.case_id in passed_before for c in report.regressions)
    assert all(c.after in FAILING for c in report.regressions)
    unchanged = (
        run.summary.total - len(report.regressions) - len(report.improvements) - len(report.changed)
    )
    assert unchanged > 0, "cases that did not change must not be reported"
    assert report.exit_code == 1
    assert (tmp_path / "check-flawed" / "check.json").exists()


def test_check_without_baseline_is_an_error(suite: Suite, tmp_path: Path) -> None:
    report, run = check(suite, ScriptedAgent(), tmp_path / "check", baseline_dir=tmp_path / "none")
    assert report.exit_code == 2 and run is None and "no baseline" in report.message


def test_check_with_crashing_agent_exits_2(suite: Suite, tmp_path: Path) -> None:
    baseline_dir = tmp_path / "baselines"
    approve(
        run_replay(suite, ScriptedAgent(), tmp_path / "base", workers=3), "reviewer", baseline_dir
    )
    report, run = check(suite, _Crashing(), tmp_path / "check", baseline_dir=baseline_dir)
    assert report.reran and report.exit_code == 2 and report.errors == run.summary.total


def test_improvements_and_accepted_count_as_good(suite: Suite, tmp_path: Path) -> None:
    baseline_dir = tmp_path / "baselines"
    flawed_run = run_replay(
        suite, ScriptedAgent(flaws=["superseded_procedure"]), tmp_path / "base", workers=3
    )
    approve(flawed_run, "reviewer", baseline_dir)
    report, run = check(suite, ScriptedAgent(), tmp_path / "check", baseline_dir=baseline_dir)
    # Post-change tickets improve. Pre-change tickets regress: the flawed agent matched their
    # outdated history by accident, the fixed agent does not. Those carry the visible tag.
    assert report.improvements
    tagged = {r.case_id for r in run.results if "pre-procedure-change" in r.tags}
    assert all(c.case_id in tagged for c in report.regressions), report.regressions
    assert report.exit_code == (1 if report.regressions else 0)
    # Accepting the remaining failures by case makes a later check treat them as good.
    store = AcceptedDifferences()
    for r in run.results:
        if r.outcome in ACCEPTABLE:
            store.add(
                "case",
                r.case_id,
                "history predates the KRaft move",
                "reviewer",
                kind="outdated_history",
            )
    report2, run2 = check(
        suite,
        ScriptedAgent(),
        tmp_path / "check2",
        baseline_dir=baseline_dir,
        accepted=store,
        force=True,
    )
    assert run2.summary.failed == 0 and run2.summary.accepted == sum(
        r.outcome in ACCEPTABLE for r in run.results
    )


def test_fingerprint_covers_agent_test_set_and_tools(suite: Suite, small_dataset: Path) -> None:
    other = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=10
    )
    a = compute_fingerprint(ScriptedAgent().config, suite.sha256(), "tools")
    assert a != compute_fingerprint(
        ScriptedAgent(flaws=["unsafe_write"]).config, suite.sha256(), "tools"
    )
    assert a != compute_fingerprint(ScriptedAgent().config, other.sha256(), "tools")
    assert a != compute_fingerprint(ScriptedAgent().config, suite.sha256(), "tools-v2")
    assert a == compute_fingerprint(ScriptedAgent().config, suite.sha256(), "tools")


def test_cli_approve_and_check_exit_codes(small_dataset: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    ts = tmp_path / "ts.json"
    baselines = tmp_path / "baselines"
    assert (
        runner.invoke(
            app,
            [
                "build-testset",
                "--type",
                "artifactory_repo",
                "--data",
                str(small_dataset),
                "--n",
                "12",
                "--out",
                str(ts),
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
                str(ts),
                "--out",
                str(tmp_path / "base"),
                "--accepted-file",
                str(tmp_path / "acc.yaml"),
            ],
        ).exit_code
        == 0
    )
    missing = runner.invoke(
        app,
        [
            "check",
            "--testset",
            str(ts),
            "--out",
            str(tmp_path / "c0"),
            "--baseline-dir",
            str(baselines),
            "--accepted-file",
            str(tmp_path / "acc.yaml"),
        ],
    )
    assert missing.exit_code == 2, missing.output
    approved = runner.invoke(
        app,
        [
            "approve",
            "--run",
            str(tmp_path / "base"),
            "--approver",
            "reviewer",
            "--baseline-dir",
            str(baselines),
        ],
    )
    assert approved.exit_code == 0, approved.output
    same = runner.invoke(
        app,
        [
            "check",
            "--testset",
            str(ts),
            "--out",
            str(tmp_path / "c1"),
            "--baseline-dir",
            str(baselines),
            "--accepted-file",
            str(tmp_path / "acc.yaml"),
        ],
    )
    assert same.exit_code == 0 and "unchanged" in same.output
    assert "baseline: passed " in same.output, "an unchanged check still states the result"
    flawed = runner.invoke(
        app,
        [
            "check",
            "--testset",
            str(ts),
            "--out",
            str(tmp_path / "c2"),
            "--flaws",
            "guess_missing_info",
            "--baseline-dir",
            str(baselines),
            "--accepted-file",
            str(tmp_path / "acc.yaml"),
        ],
    )
    assert flawed.exit_code == 1, flawed.output
    assert "REGRESSION" in flawed.output
