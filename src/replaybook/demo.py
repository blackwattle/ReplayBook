"""`replaybook demo`: the whole story, offline, in one directory.

1. Generate synthetic tickets with hidden ground truth.
2. Build test sets for three request types from that history.
3. Replay a reference agent seeded with known flaws and catch them.
4. Group the failures by cause with two side-by-side examples each.
5. Accept the cases where history itself is outdated (reason, approver, expiry).
6. Approve the flawed run as the baseline, fix the flaws, and show only what changed.
7. Write the evidence pack: HTML, JSON and a hashed manifest.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import FLAWS, ScriptedAgent
from replaybook.discover import discover, write_report
from replaybook.eval import quality
from replaybook.eval.accepted import AcceptedDifferences, Kind
from replaybook.eval.baseline import approve, check, load_baseline
from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence
from replaybook.eval.groups import group_failures
from replaybook.eval.runner import RunResult, run_replay
from replaybook.eval.scoring import REASON_LABELS
from replaybook.eval.testset import build_test_set
from replaybook.mock_env.knowledge import Corpus
from replaybook.packs import registry
from replaybook.schema.testcase import TestSet
from replaybook.synth.generator import GenerateOptions, generate

DEMO_APPROVER = "demo-approver (automated for the demo; a person signs in real use)"


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
        f"two procedure changes). The answers are hidden in a separate file that only the "
        f"eval module reads."
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
            f"to ask "
            f"for missing information, {sel['tagged']['pre-procedure-change']} predate a procedure "
            f"change, {sel['tagged']['corrected-later']} were corrected by a later ticket"
        )

    # 3. Flawed agent
    flawed = ScriptedAgent(flaws=FLAWS)
    changes = "; ".join(
        f"{packs.label(c.request_type)}: {c.from_version} -> {c.to_version}"
        for c in packs.procedure_changes()
    )
    say(
        "\n3. Replayed the reference agent with three seeded flaws (guesses missing information, "
        f"follows the procedures from before the last change ({changes}), and takes a "
        "destructive shortcut when a resource already exists):"
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
        for r in ("guessed_missing_info", "wrong_tool", "unsafe_action")
        if r in caught
    ]
    say(f"   Caught: {', '.join(labels)}. Any critical unsafe action fails the gate on its own.")

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
        )
        assert run is not None
        exit_codes.append(report.exit_code)
        say(
            f"   {request_type}: {len(report.improvements)} improved, "
            f"{len(report.regressions)} regressed, "
            f"{len(report.changed)} changed reason; now {_line(run)}; exit code {report.exit_code}"
        )
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
        groups = group_failures(flawed_runs[request_type].results)
        if groups:
            purity = quality.group_purity(groups, flawed_runs[request_type], data_dir)
            quality_report[f"{request_type}: v1 group purity vs seeded flaws"] = f"{purity:.2f}"

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
                "agent "
                "with three seeded flaws (v1)."
            ),
            today=today,
        ),
        evidence_dir,
    )
    elapsed = time.perf_counter() - started
    say(
        f"\n7. Wrote the evidence pack: {paths['html']} "
        "(plus evidence.json and a SHA-256 manifest)."
    )
    say(f"\nDone in {elapsed:.1f}s. Open {paths['html']} in a browser.")
    return {
        "elapsed_s": elapsed,
        "exit_codes": exit_codes,
        "quality": quality_report,
        "evidence": {k: str(v) for k, v in paths.items()},
        "accepted": len(store.accepted),
        "caught": sorted(caught),
    }


def _line(run: RunResult) -> str:
    s = run.summary
    return (
        f"{s.passed} pass, {s.accepted} accepted, {s.failed} fail, {s.errors} error of {s.total} "
        f"(pass rate {s.pass_rate:.0%}, 95% CI {s.wilson_low:.0%}-{s.wilson_high:.0%}), "
        f"critical {s.critical_failures}, gate {s.gate.upper()}"
    )


def _change_reason(platform: dict[str, Any], request_type: str) -> str:
    for change in platform.get("procedure_changes", []):
        if change.get("request_type") == request_type:
            return (
                f"Ticket predates the {change['from_version']} to {change['to_version']} change on "
                f"{str(change['at'])[:10]}; engineers followed the procedure of the time."
            )
    return "Ticket predates a procedure change; engineers followed the procedure of the time."
