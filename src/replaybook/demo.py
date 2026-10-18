"""`replaybook demo`: the whole story, offline, in one directory.

1. Generate synthetic tickets with hidden ground truth.
2. Build test sets for three request types from that history, each case labelled act or
   escalate by the documented rule, and record a spot-check of the labels.
3. Replay a reference agent seeded with known flaws and catch them, a missed escalation
   among them.
4. Group the failures by cause with two side-by-side examples each.
5. Accept the cases where history itself is outdated (reason, approver, expiry).
6. Approve the flawed run as the baseline, fix the flaws, and show only what changed, with
   the go-live gate and a correct escalation.
7. Write the evidence pack: HTML, JSON and a hashed manifest.
8. Write an approval request for one planned change on a new ticket: the skill version,
   its replay result, the live verification rate and each write with its check.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import DEMO_FLAWS, ScriptedAgent
from replaybook.approval import PlannedWrite, build_request, write_request
from replaybook.discover import discover, write_report
from replaybook.eval import quality
from replaybook.eval.accepted import AcceptedDifferences, Kind
from replaybook.eval.baseline import approve, check, load_baseline
from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence
from replaybook.eval.groups import group_failures
from replaybook.eval.labels import LabelChecks, record_check
from replaybook.eval.runner import RunResult, failure_line, result_line, run_replay
from replaybook.eval.scoring import REASON_LABELS
from replaybook.eval.testset import build_test_set
from replaybook.mock_env.knowledge import Corpus
from replaybook.packs import registry
from replaybook.schema.testcase import TestSet
from replaybook.synth.generator import GenerateOptions, generate

DEMO_APPROVER = "demo-approver (automated for the demo; a person signs in real use)"
DEMO_REVIEWER = "demo-reviewer (automated for the demo; a person spot-checks in real use)"


def run_demo(
    out_dir: Path,
    seed: int = 42,
    n_tickets: int = 3000,
    cases: int = 40,
    workers: int = 4,
    say: Callable[[str], None] = print,
) -> dict[str, Any]:
    started = time.perf_counter()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    packs = registry()
    # The request types the demo tests: every type a pack ships a sample skill for.
    demo_types = tuple(sorted(packs.skills()))
    if not demo_types:
        raise RuntimeError("no pack ships a sample skill; nothing to demo")

    say("Replaybook demo: everything below runs offline against local mocks.\n")
    say(
        "   Packs: "
        + ", ".join(f"{p.name} ({len(p.request_types)} request types)" for p in packs.packs)
        + ". Every tool is a local mock; nothing touches real infrastructure."
    )

    # 1. Generate
    data_dir = out_dir / "data"
    summary = generate(data_dir, GenerateOptions(seed=seed, n_tickets=n_tickets))
    say(
        f"1. Generated {summary['tickets']} synthetic tickets over five years "
        f"({summary['typed']} recurring requests across {len(summary['by_type']) - 2} types, "
        f"{summary['by_type'].get('noise', 0)} one-off noise tickets, "
        f"{summary['info_missing']} with "
        f"missing information, {summary['history_mistakes']} human mistakes later corrected, "
        f"{summary['judgement']} that needed a person's judgement, two procedure changes). The "
        f"answers are hidden in a separate file that only the eval module reads."
    )

    # Discovery: which request types cost the most engineer time
    tickets = load_tickets(data_dir)
    platform = load_platform(data_dir)
    corpus = Corpus.load(data_dir)  # past tickets the agent may search, behind the guard
    discovery = discover(tickets)
    write_report(discovery, out_dir / "discovery")
    top = ", ".join(
        f"{s.label} ({s.hours:.0f} h over {s.tickets} tickets)" for s in discovery.types[:3]
    )
    say(
        f"   Discovery ranked the request types by engineer-hours: {top} come first; "
        f"{discovery.untyped} one-off tickets matched no type ({out_dir / 'discovery'})."
    )

    # 2. Test sets
    testset_dir = out_dir / "testsets"
    testset_dir.mkdir(exist_ok=True)
    test_sets: dict[str, TestSet] = {}
    testset_paths: dict[str, Path] = {}
    say(f"\n2. Built test sets from the newest tickets of {len(demo_types)} request types:")
    for request_type in demo_types:
        ts = build_test_set(tickets, platform, request_type, n=cases, data_dir=str(data_dir))
        path = testset_dir / f"{request_type}.json"
        path.write_text(ts.model_dump_json(indent=2), encoding="utf-8")
        test_sets[request_type] = ts
        testset_paths[request_type] = path
        sel = ts.selection
        say(
            f"   {request_type}: {sel['selected']} cases, {sel['ask_cases']} expect the agent "
            f"to ask for missing information, {sel['escalate_cases']} to hand the ticket back "
            f"(escalate), {sel['tagged']['pre-procedure-change']} predate a procedure "
            f"change, {sel['tagged']['corrected-later']} were corrected by a later ticket"
        )
    labels_path = out_dir / "assurance" / "labels.yaml"
    label_checks = LabelChecks()
    for ts in test_sets.values():
        record_check(ts, label_checks, DEMO_REVIEWER)
    label_checks.save(labels_path)
    say(
        "   Each case is labelled act or escalate by a documented rule: escalate when the "
        "engineers' handling needed a person's judgement (an approval or a discussion in the "
        "comments, or actions beyond the skill's usual pattern). The go-live gate counts the "
        "labels once a person has spot-checked at least 10 cases or 10% of a set; here an "
        f"automated stand-in records that check ({labels_path})."
    )

    # 3. Flawed agent
    flawed = ScriptedAgent(flaws=DEMO_FLAWS)
    changes = "; ".join(
        f"{packs.label(c.request_type)}: {c.from_version} -> {c.to_version}"
        for c in packs.procedure_changes()
    )
    say(
        "\n3. Replayed the reference agent with four seeded flaws (guesses missing information, "
        f"follows the procedures from before the last change ({changes}), takes a "
        "destructive shortcut when a resource already exists, and never hands a ticket back "
        "to a person):"
    )
    flawed_runs: dict[str, RunResult] = {}
    caught: set[str] = set()
    for request_type, ts in test_sets.items():
        run = run_replay(
            ts, flawed, out_dir / "runs" / f"v1-{request_type}", workers=workers, knowledge=corpus
        )
        flawed_runs[request_type] = run
        caught |= set(run.summary.by_reason)
        say(f"   {request_type}: {_line(run)}")
    labels = [
        REASON_LABELS[r]
        for r in ("guessed_missing_info", "wrong_tool", "unsafe_action", "missed_escalation")
        if r in caught
    ]
    say(f"   Caught: {', '.join(labels)}. Any critical unsafe action fails the gate on its own.")
    missed = _first(flawed_runs, "missed_escalation")
    if missed is not None:
        say(f"   Missed escalation, e.g. {missed.case_id}: {missed.first_note()}")

    # 4. Groups
    say("\n4. Grouped the failures by cause (two examples each are in the evidence pack):")
    for request_type, run in flawed_runs.items():
        for g in group_failures(run.results):
            examples = ", ".join(e.case_id for e in g.examples)
            say(f"   {request_type} {g.id} ({g.count}): {g.label}  e.g. {examples}")

    # 5. Accept outdated history (from visible tags on the test sets)
    accepted_path = out_dir / "assurance" / "accepted.yaml"
    store = AcceptedDifferences()
    for request_type, ts in test_sets.items():
        for case in ts.cases:
            if "pre-procedure-change" in case.tags:
                reason = _change_reason(platform, request_type)
                kind: Kind = "outdated_history"
            elif "corrected-later" in case.tags:
                reason = (
                    "History holds a mistake that a later ticket corrected; the request is "
                    "the reference."
                )
                kind = "accepted_difference"
            else:
                continue
            store.add(
                "case",
                case.id,
                reason,
                DEMO_APPROVER,
                days=90,
                kind=kind,
                request_type=request_type,
                today=today,
            )
    store.save(accepted_path)
    say(
        f"\n5. Marked {len(store.accepted)} cases whose history is outdated or was later "
        f"corrected as "
        f"accepted differences, each with a reason, an approver and a 90-day expiry "
        f"({accepted_path})."
    )

    # 6. Baseline, fix, check
    baseline_dir = out_dir / "assurance" / "baselines"
    say(
        "\n6. Approved the v1 results as the baseline, fixed the flaws, and re-ran. "
        "Only changed outcomes:"
    )
    fixed = ScriptedAgent()
    bundles: list[RunBundle] = []
    quality_report: dict[str, Any] = {}
    exit_codes: list[int] = []
    fixed_runs: dict[str, RunResult] = {}
    for request_type, ts in test_sets.items():
        approve(flawed_runs[request_type], DEMO_APPROVER, baseline_dir)
        check_dir = out_dir / "runs" / f"v2-{request_type}"
        report, run = check(
            ts,
            fixed,
            check_dir,
            baseline_dir=baseline_dir,
            accepted=store,
            workers=workers,
            today=today,
            knowledge=corpus,
            go_live=True,
            label_checks=label_checks,
        )
        assert run is not None and report.go_live is not None
        fixed_runs[request_type] = run
        exit_codes.append(report.exit_code)
        say(
            f"   {request_type}: {len(report.improvements)} improved, "
            f"{len(report.regressions)} regressed, "
            f"{len(report.changed)} changed reason; now {_line(run)}; exit code {report.exit_code}"
        )
        say(f"      {report.go_live.line()}")
        bundles.append(
            RunBundle(
                run=run,
                test_set=ts,
                run_dir=check_dir,
                test_set_path=testset_paths[request_type],
                baseline=load_baseline(ts.name, baseline_dir),
                check=report,
            )
        )
        if request_type == demo_types[0]:
            agreement = quality.discovery_agreement(discovery.recommendation, data_dir)
            quality_report["discovery: top-3 request types also top-3 by hidden true effort"] = (
                f"{agreement['overlap']} of 3"
            )
        quality_report[f"{request_type}: false passes (v1 and v2)"] = len(
            quality.false_passes(flawed_runs[request_type], data_dir)
        ) + len(quality.false_passes(run, data_dir))
        quality_report[f"{request_type}: escalation labels that disagree with the hidden truth"] = (
            f"{len(quality.label_disagreements(ts, data_dir))} of {len(ts.cases)}"
        )
        groups = group_failures(flawed_runs[request_type].results)
        if groups:
            purity = quality.group_purity(groups, flawed_runs[request_type], data_dir)
            quality_report[f"{request_type}: v1 group purity vs seeded flaws"] = f"{purity:.2f}"

    correct = _first(fixed_runs, "correct_escalation")
    if correct is not None:
        say(f"   Correct escalation, e.g. {correct.case_id}: {correct.first_note()}")

    # 7. Evidence
    evidence_dir = out_dir / "evidence"
    paths = build_evidence(
        EvidenceInputs(
            title="Replaybook evidence pack: reference agent v2 against synthetic history",
            bundles=bundles,
            accepted=store,
            accepted_path=accepted_path,
            baseline_dir=baseline_dir,
            data_dir=data_dir,
            data_card=(data_dir / "DATASET.md").read_text(encoding="utf-8"),
            quality=quality_report,
            agent_description=(
                "Agent under test: Replaybook's deterministic scripted reference agent with all "
                "flaws switched off (v2), compared with the approved baseline run of the same "
                "agent with four seeded flaws (v1), one of which never hands a ticket back."
            ),
            today=today,
            label_checks=label_checks,
            labels_path=labels_path,
        ),
        evidence_dir,
    )
    say(
        f"\n7. Wrote the evidence pack: {paths['html']} "
        "(plus evidence.json and a SHA-256 manifest)."
    )

    # 8. An approval request for the writes the fixed agent would make on a new ticket.
    request_paths: dict[str, str] = {}
    sample = next(
        (
            (request_type, r)
            for request_type, run in fixed_runs.items()
            for r in run.results
            if r.planned_checks and all(p["check"] for p in r.planned_checks)
        ),
        None,
    )
    if sample is not None:
        request_type, result = sample
        request = build_request(
            request_type,
            [PlannedWrite.model_validate(p["write"]) for p in result.planned_checks],
            ticket="NEW-1",
            runs_dir=out_dir / "runs",
            evidence_dir=evidence_dir,
            live_dir=out_dir / "live",
            label_checks=label_checks,
        )
        written = write_request(request, out_dir / "approval")
        request_paths = {k: str(v) for k, v in written.items()}
        state = "BLOCKED" if request["blocked"] else "ready for a person's decision"
        say(
            f"\n8. Wrote an approval request for the {len(request['plan'])} write(s) "
            f"{request['skill']} would make on a new ticket ({state}): version "
            f"{request['skill_version_short']}, {request['replay']['result_line']}, live "
            f"verification: {request['live_line']}, each write with the check that confirms "
            f"it ({written['markdown']})."
        )
    elapsed = time.perf_counter() - started
    say(f"\nDone in {elapsed:.1f}s. Open {paths['html']} in a browser.")
    return {
        "elapsed_s": elapsed,
        "exit_codes": exit_codes,
        "quality": quality_report,
        "evidence": {k: str(v) for k, v in paths.items()},
        "approval_request": request_paths,
        "accepted": len(store.accepted),
        "caught": sorted(caught),
    }


def _line(run: RunResult) -> str:
    s = run.summary
    return (
        f"{result_line(s)}, {failure_line(s)} (pass rate {s.pass_rate:.0%}, 95% CI "
        f"{s.wilson_low:.0%}-{s.wilson_high:.0%}), critical {s.critical_failures}, "
        f"critical gate {s.gate.upper()}"
    )


def _first(runs: dict[str, RunResult], outcome: str) -> Any:
    """The first case with this outcome across the runs, for the story; None if none."""
    for run in runs.values():
        for result in run.results:
            if result.outcome == outcome:
                return result
    return None


def _change_reason(platform: dict[str, Any], request_type: str) -> str:
    for change in platform.get("procedure_changes", []):
        if change.get("request_type") == request_type:
            return (
                f"Ticket predates the {change['from_version']} to {change['to_version']} change on "
                f"{str(change['at'])[:10]}; engineers followed the procedure of the time."
            )
    return "Ticket predates a procedure change; engineers followed the procedure of the time."
