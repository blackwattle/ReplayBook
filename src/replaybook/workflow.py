"""The workflow as plain functions, shared by the CLI and by `replaybook mcp`.

Each function takes the same arguments as the CLI command of the same name, does the same
work on the same files on disk, and returns a JSON-able dict with a one-line `message`.
The CLI prints; the MCP server returns the dict to whichever assistant is operating
Replaybook. Nothing here prints, and nothing here knows about typer or MCP.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path("data/generated")
DEFAULT_ACCEPTED = Path("assurance/accepted.yaml")
DEFAULT_BASELINES = Path("assurance/baselines")
DEFAULT_LABELS = Path("assurance/labels.yaml")


class WorkflowError(ValueError):
    """Bad arguments or missing files; the CLI turns it into a usage error, the MCP server
    into an error result."""


# --- agents and knowledge ----------------------------------------------------------------


def make_agent(
    kind: str,
    flaws: str = "",
    command: str = "",
    request_type: str = "",
    transport: str = "stdio",
) -> Any:
    """Build an agent under test: scripted, skill, external, or a launcher by name."""
    if kind == "scripted":
        from replaybook.agents.scripted import ScriptedAgent

        try:
            return ScriptedAgent(flaws=[f for f in flaws.split(",") if f])
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
    if kind == "skill":
        from replaybook.agents.skill_agent import SkillAgent
        from replaybook.llm.client import LLMClient

        try:
            return SkillAgent(request_type, LLMClient())
        except KeyError as exc:
            raise WorkflowError(str(exc)) from exc
    if kind == "external":
        from replaybook.agents.external import ExternalAgent

        if not command:
            raise WorkflowError("--command is required for the external agent")
        try:
            return ExternalAgent(shlex.split(command), transport=transport)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
    from replaybook.agents.launchers import LauncherAgent, LauncherError, launchers

    try:
        launcher = launchers().launcher(kind)
    except LauncherError as exc:
        raise WorkflowError(f"unknown agent '{kind}': {exc}") from exc
    # --command overrides the harness executable (a wrapper script, a different install).
    return LauncherAgent(launcher, request_type, executable=shlex.split(command) or None)


def knowledge_for(test_set: Any, option: Path | str | None) -> Any:
    """The past-ticket corpus for a replay: the option, else the test set's data_dir;
    'none' attaches nothing."""
    from replaybook.mock_env.knowledge import Corpus

    if option is not None and str(option).lower() == "none":
        return None
    return Corpus.maybe(option if option is not None else test_set.data_dir)


def load_test_set(path: Path) -> Any:
    from replaybook.schema.testcase import TestSet

    path = Path(path)
    if not path.is_file():
        raise WorkflowError(f"no test set at {path}")
    return TestSet.model_validate_json(path.read_text(encoding="utf-8"))


def summary_line(run: Any, gate: Any = None) -> str:
    """The result line ('passed X of N (A matched, B correctly escalated, C accepted)'), the
    failures by outcome, the unneeded-escalation rate, the critical gate and, when given,
    the go-live gate."""
    from replaybook.eval.runner import failure_line, result_line, usefulness_line
    from replaybook.skills import short

    s = run.summary
    flaws = run.agent_config.get("flaws") or []
    flaw_text = " flaws=" + ",".join(flaws) if flaws else ""
    agent = run.agent_config.get("launcher") or run.agent_config.get("agent")
    skill = f" (skill {run.skill} {short(run.skill_version)})" if run.skill else ""
    line = (
        f"{run.test_set_name} / {agent}{flaw_text}{skill}: {result_line(s)}; "
        f"{failure_line(s)} (pass rate {s.pass_rate:.0%}, 95% CI "
        f"{s.wilson_low:.0%}-{s.wilson_high:.0%}); {usefulness_line(s)}; "
        f"critical {s.critical_failures}; critical gate {s.gate.upper()}"
    )
    if s.by_reason:
        line += "\n  failures by reason: " + ", ".join(f"{k} {v}" for k, v in s.by_reason.items())
    if gate is not None:
        line += "\n  " + gate.line()
    return line


def go_live_for(run: Any, labels_file: Path | str | None, min_pass_rate: float | None) -> Any:
    """The go-live gate for a finished run, with the label spot-checks in `labels_file`."""
    from replaybook.eval.golive import gate_for_run, load_label_checks

    try:
        return gate_for_run(
            run, load_label_checks(Path(labels_file) if labels_file else None), min_pass_rate
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


# --- the commands --------------------------------------------------------------------------


def generate(out: Path = DEFAULT_DATA_DIR, seed: int = 42, n: int = 3000) -> dict[str, Any]:
    """Generate synthetic tickets with hidden ground truth."""
    from replaybook.synth import generator

    out = Path(out)
    summary = generator.generate(out, generator.GenerateOptions(seed=seed, n_tickets=n))
    return {
        "message": (
            f"Wrote {summary['tickets']} tickets to {out} ({summary['typed']} typed, "
            f"{summary['by_type'].get('noise', 0)} noise, {summary['info_missing']} with "
            "missing info)."
        ),
        "out": str(out),
        "tickets": summary["tickets"],
        "typed": summary["typed"],
        "noise": summary["by_type"].get("noise", 0),
        "info_missing": summary["info_missing"],
        "seed": seed,
    }


def build_testset(
    request_type: str,
    data: Path = DEFAULT_DATA_DIR,
    n: int = 40,
    out: Path | None = None,
    extract: str = "none",
) -> dict[str, Any]:
    """Build a test set from the newest tickets of one request type."""
    from replaybook.adapters.synthetic import load_platform, load_tickets
    from replaybook.eval.extract import make_extractor
    from replaybook.eval.testset import build_test_set

    data = Path(data)
    if not (data / "tickets.jsonl").is_file():
        raise WorkflowError(f"no tickets.jsonl in {data}")
    tickets = load_tickets(data)
    source = tickets[0].source if tickets else "synthetic"
    try:
        extractor = make_extractor(extract)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    test_set = build_test_set(
        tickets,
        load_platform(data),
        request_type,
        n=n,
        source=source,
        data_dir=str(data),
        extractor=extractor,
    )
    out = Path(out) if out else Path("testsets") / f"{request_type}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(test_set.model_dump_json(indent=2), encoding="utf-8")
    sel = test_set.selection
    unverified = f", {sel['unverified']} unverified" if sel.get("unverified") else ""
    return {
        "message": (
            f"{test_set.name}: {sel['selected']} cases ({sel['ask_cases']} expect a question"
            f"{unverified}), from {sel['eligible']} eligible tickets; "
            f"excluded {sel['excluded']} -> {out}"
        ),
        "path": str(out),
        "name": test_set.name,
        "request_type": request_type,
        "cases": len(test_set.cases),
        "selection": sel,
    }


def replay(
    testset: Path,
    out: Path,
    agent: str = "scripted",
    flaws: str = "",
    command: str = "",
    workers: int = 4,
    seed: int = 0,
    resume: bool = True,
    accepted_file: Path = DEFAULT_ACCEPTED,
    knowledge: Path | str | None = None,
    transport: str = "stdio",
    labels_file: Path = DEFAULT_LABELS,
    min_pass_rate: float | None = None,
) -> dict[str, Any]:
    """Replay an agent against a test set and score every case. The exit code follows the
    critical gate (no critical unsafe action, no agent error); the go-live gate is reported
    beside it."""
    from replaybook.checks import SkillInvalid
    from replaybook.eval.accepted import AcceptedDifferences
    from replaybook.eval.runner import run_replay

    test_set = load_test_set(testset)
    try:
        run = run_replay(
            test_set,
            make_agent(agent, flaws, command, test_set.request_type, transport),
            Path(out),
            workers=workers,
            resume=resume,
            seed=seed,
            accepted=AcceptedDifferences.load(Path(accepted_file)),
            knowledge=knowledge_for(test_set, knowledge),
        )
    except SkillInvalid as exc:
        raise WorkflowError(str(exc)) from exc
    gate = go_live_for(run, labels_file, min_pass_rate)
    return _run_dict(run, Path(out), gate) | {"exit_code": 0 if run.summary.gate == "pass" else 1}


def _run_dict(run: Any, run_dir: Path, gate: Any = None) -> dict[str, Any]:
    from replaybook.eval.golive import gate_dict
    from replaybook.eval.runner import result_line

    return {
        "message": summary_line(run, gate),
        "run_dir": str(run_dir),
        "run_id": run.run_id,
        "test_set": run.test_set_name,
        "request_type": run.test_set_request_type,
        "agent_config": run.agent_config,
        "skill": run.skill,
        "skill_version": run.skill_version,
        "fingerprint": run.fingerprint,
        "summary": run.summary.model_dump(),
        "result_line": result_line(run.summary),
        "gate": run.summary.gate,
        "go_live": gate_dict(gate) if gate is not None else None,
    }


def report(
    run_dir: Path, labels_file: Path = DEFAULT_LABELS, min_pass_rate: float | None = None
) -> dict[str, Any]:
    """The summary, the go-live gate and the non-passing cases of a finished run."""
    from replaybook.eval.runner import load_run
    from replaybook.eval.scoring import PASSING

    run_dir = Path(run_dir)
    if not (run_dir / "results.json").is_file():
        raise WorkflowError(f"no results.json in {run_dir}; run `replaybook replay` first")
    run = load_run(run_dir)
    gate = go_live_for(run, labels_file, min_pass_rate)
    non_passing = [
        {
            "case_id": r.case_id,
            "ticket_id": r.ticket_id,
            "outcome": r.outcome,
            "reason": r.reason,
            "severity": r.severity,
            "short": r.short(),
            "note": r.accepted_reason or r.first_note(),
            "tags": list(r.tags),
            "expected_outcome": r.expected_outcome,
        }
        for r in run.results
        if r.outcome not in PASSING or r.outcome == "accepted"
    ]
    escalations = [
        {
            "case_id": r.case_id,
            "outcome": r.outcome,
            "reason": r.escalated_with,
            "why": "; ".join(r.escalation_reasons),
        }
        for r in run.results
        if r.outcome == "correct_escalation"
    ]
    return _run_dict(run, run_dir, gate) | {
        "non_passing": non_passing,
        "correct_escalations": escalations,
    }


def groups(run_dir: Path, model: bool = False, llm: Any = None) -> dict[str, Any]:
    """Group a run's failures by cause, with two examples each, and save groups.json. With
    `model`, the fast model writes each group's cause; the rule text stays when the model is
    offline or at the spend cap, and the result says which wrote each cause. `llm` replaces
    the model client (tests pass a fake)."""
    from replaybook.eval.groups import cause_line, describe_with_model, group_failures
    from replaybook.eval.runner import load_run

    run_dir = Path(run_dir)
    if not (run_dir / "results.json").is_file():
        raise WorkflowError(f"no results.json in {run_dir}; run `replaybook replay` first")
    run = load_run(run_dir)
    found = group_failures(run.results)
    message = f"{len(found)} failure group(s) in {run.run_id}" if found else "No failures to group."
    if model and found:
        if llm is None:
            from replaybook.llm.client import LLMClient

            llm = LLMClient()
        describe_with_model(found, llm, tier="fast")
        message += f"; {cause_line(found)}"
        if hasattr(llm, "spend_line"):
            message += f"; {llm.spend_line()}"
    path = run_dir / "groups.json"
    path.write_text("[" + ",\n".join(g.model_dump_json() for g in found) + "]", encoding="utf-8")
    return {
        "message": f"{message} -> {path}" if found else message,
        "run_dir": str(run_dir),
        "path": str(path),
        "groups": [
            {
                "id": g.id,
                "count": g.count,
                "label": g.label,
                "cause": g.cause,
                "cause_source": g.cause_source,
                "cause_model": g.cause_model,
                "cause_note": g.cause_note,
                "reason": g.reason,
                "severity": g.severity,
                "likely_history_outdated": g.likely_history_outdated,
                "case_ids": list(g.case_ids),
                "examples": [
                    {"case_id": e.case_id, "summary": e.summary, "detail": e.detail}
                    for e in g.examples
                ],
            }
            for g in found
        ],
    }


def check(
    testset: Path,
    out: Path,
    agent: str = "scripted",
    flaws: str = "",
    command: str = "",
    workers: int = 4,
    force: bool = False,
    baseline_dir: Path = DEFAULT_BASELINES,
    accepted_file: Path = DEFAULT_ACCEPTED,
    knowledge: Path | str | None = None,
    transport: str = "stdio",
    go_live: bool = False,
    labels_file: Path = DEFAULT_LABELS,
    min_pass_rate: float | None = None,
) -> dict[str, Any]:
    """Re-run when the fingerprint changed and report only what differs from the baseline.
    Exit codes: 0 pass, 1 regression or critical failure, 2 error. With `go_live` the
    go-live gate must pass too, or the exit code is 1."""
    from replaybook.eval.accepted import AcceptedDifferences
    from replaybook.eval.baseline import check as run_check
    from replaybook.eval.golive import gate_dict, load_label_checks

    test_set = load_test_set(testset)
    try:
        report_, run_ = run_check(
            test_set,
            make_agent(agent, flaws, command, test_set.request_type, transport),
            Path(out),
            baseline_dir=Path(baseline_dir),
            accepted=AcceptedDifferences.load(Path(accepted_file)),
            force=force,
            workers=workers,
            knowledge=knowledge_for(test_set, knowledge),
            go_live=go_live,
            label_checks=load_label_checks(Path(labels_file)),
            min_pass_rate=min_pass_rate,
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    lines = [f"{report_.test_set_name}: {report_.message}"]
    for change in report_.regressions:
        lines.append(
            f"  REGRESSION {change.case_id}: {change.before} -> {change.after} "
            f"({change.reason_after}) {change.detail}"
        )
    for change in report_.changed:
        lines.append(
            f"  CHANGED    {change.case_id}: {change.reason_before} -> {change.reason_after}"
        )
    for change in report_.improvements:
        lines.append(f"  IMPROVED   {change.case_id}: {change.before} -> {change.after}")
    for case_id in report_.new_cases:
        lines.append(f"  NEW        {case_id}")
    for case_id in report_.removed_cases:
        lines.append(f"  REMOVED    {case_id}")
    if report_.reran and report_.critical_failures:
        lines.append(f"  critical failures now: {report_.critical_failures}")
    if run_ is not None:
        lines.append(summary_line(run_))
    elif report_.summary_baseline is not None and report_.summary_baseline.outcomes:
        from replaybook.eval.runner import result_line

        lines.append(f"  baseline: {result_line(report_.summary_baseline)}")
    if report_.go_live is not None:
        lines.append(report_.go_live.line())
    return {
        "message": "\n".join(lines),
        "run_dir": str(out),
        "exit_code": report_.exit_code,
        "reran": report_.reran,
        "fingerprint_changed": report_.fingerprint_changed,
        "regressions": [c.model_dump() for c in report_.regressions],
        "improvements": [c.model_dump() for c in report_.improvements],
        "changed": [c.model_dump() for c in report_.changed],
        "new_cases": list(report_.new_cases),
        "removed_cases": list(report_.removed_cases),
        "critical_failures": report_.critical_failures,
        "summary": run_.summary.model_dump() if run_ is not None else None,
        "go_live": gate_dict(report_.go_live) if report_.go_live is not None else None,
    }


def evidence(
    run: list[Path],
    out: Path,
    testset: list[Path] | None = None,
    title: str = "Replaybook evidence pack",
    baseline_dir: Path = DEFAULT_BASELINES,
    accepted_file: Path = DEFAULT_ACCEPTED,
    shadow: Path | None = None,
    labels_file: Path = DEFAULT_LABELS,
    min_pass_rate: float | None = None,
    live: Path | None = None,
) -> dict[str, Any]:
    """Write the evidence pack (HTML, JSON and SHA-256 manifest) for one or more runs. Its
    verdict is the go-live gate; `gate` in the result says it. `live` adds the go-live
    metrics from a live-runs directory."""
    from replaybook.eval.accepted import AcceptedDifferences
    from replaybook.eval.baseline import CheckReport, load_baseline
    from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence
    from replaybook.eval.golive import load_label_checks
    from replaybook.eval.golive import min_pass_rate as threshold
    from replaybook.eval.runner import load_run
    from replaybook.eval.shadow import load_report
    from replaybook.schema.testcase import TestSet

    if not run:
        raise WorkflowError("give at least one run directory")
    shadow_dir = Path(shadow) if shadow is not None else None
    shadow_report = load_report(shadow_dir) if shadow_dir is not None else None
    if shadow_dir is not None and shadow_report is None:
        raise WorkflowError(f"no shadow.json in {shadow_dir}; run `replaybook shadow record` first")
    baseline_dir = Path(baseline_dir)
    accepted_file = Path(accepted_file)
    bundles = []
    for index, run_dir in enumerate(run):
        run_dir = Path(run_dir)
        if not (run_dir / "results.json").is_file():
            raise WorkflowError(f"no results.json in {run_dir}")
        loaded = load_run(run_dir)
        if testset and index < len(testset):
            ts_path: Path | None = Path(testset[index])
            ts = load_test_set(ts_path)
        else:
            ts_path = None
            ts = TestSet(
                name=loaded.test_set_name,
                request_type=loaded.test_set_request_type,
                source="unknown",
                data_dir="",
                created_at=loaded.created_at,
            )
        check_path = run_dir / "check.json"
        check_report = (
            CheckReport.model_validate_json(check_path.read_text(encoding="utf-8"))
            if check_path.exists()
            else None
        )
        bundles.append(
            RunBundle(
                run=loaded,
                test_set=ts,
                run_dir=run_dir,
                test_set_path=ts_path,
                baseline=load_baseline(loaded.test_set_name, baseline_dir),
                check=check_report,
            )
        )
    labels_file = Path(labels_file)
    live_report_ = None
    if live is not None:
        from replaybook.eval.live import RUNS_FILE, build_report

        if not (Path(live) / RUNS_FILE).is_file():
            raise WorkflowError(
                f"no {RUNS_FILE} in {live}; record runs with `replaybook live record`"
            )
    try:
        threshold(min_pass_rate)
        if live is not None:
            live_report_ = build_report(Path(live))
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    paths = build_evidence(
        EvidenceInputs(
            title=title,
            bundles=bundles,
            accepted=AcceptedDifferences.load(accepted_file),
            accepted_path=accepted_file if accepted_file.exists() else None,
            baseline_dir=baseline_dir,
            shadow=shadow_report,
            shadow_dir=shadow_dir,
            label_checks=load_label_checks(labels_file),
            labels_path=labels_file if labels_file.exists() else None,
            min_pass_rate=min_pass_rate,
            live=live_report_,
            live_dir=Path(live) if live is not None else None,
        ),
        Path(out),
    )
    gate = "PASS" if all(b.gate is not None and b.gate.passed for b in bundles) else "FAIL"
    lines = [f"Wrote {paths['html']}, {paths['json']} and {paths['manifest']}"]
    lines += [
        f"  {b.test_set.request_type}: {b.gate.line()}" for b in bundles if b.gate is not None
    ]
    return {
        "message": "\n".join(lines),
        "html": str(paths["html"]),
        "json": str(paths["json"]),
        "manifest": str(paths["manifest"]),
        "gate": gate,
        "critical_gate": "FAIL" if any(b.run.summary.gate != "pass" for b in bundles) else "PASS",
        "runs": [b.run.run_id for b in bundles],
    }


def shadow_record(
    request_type: str,
    out: Path,
    data: Path = DEFAULT_DATA_DIR,
    agent: str = "scripted",
    flaws: str = "",
    command: str = "",
    extract: str = "none",
    knowledge: Path | str | None = None,
    transport: str = "stdio",
) -> dict[str, Any]:
    """Run the agent on every open ticket of a request type against the mock and store the
    actions it proposed. Nothing is executed and nothing is written to the ticket system."""
    from replaybook.adapters.synthetic import load_platform, load_tickets
    from replaybook.eval.extract import make_extractor
    from replaybook.eval.shadow import record
    from replaybook.mock_env.knowledge import Corpus

    data = Path(data)
    if not (data / "tickets.jsonl").is_file():
        raise WorkflowError(f"no tickets.jsonl in {data}")
    corpus = (
        None
        if (knowledge is not None and str(knowledge).lower() == "none")
        else Corpus.maybe(knowledge if knowledge is not None else data)
    )
    try:
        extractor = make_extractor(extract)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    from replaybook.checks import SkillInvalid

    try:
        stats = record(
            load_tickets(data),
            load_platform(data),
            request_type,
            make_agent(agent, flaws, command, request_type, transport),
            Path(out),
            knowledge=corpus,
            extractor=extractor,
        )
    except SkillInvalid as exc:
        raise WorkflowError(str(exc)) from exc
    return {
        "message": (
            f"{request_type}: {stats['open_tickets']} open tickets, {stats['recorded']} proposals "
            f"recorded ({stats['asked']} would ask the requester, {stats['proposed_writes']} "
            f"proposed writes), {stats['already_recorded']} already recorded for this agent -> "
            f"{out}\nNothing was executed. Score them once the tickets are resolved: "
            "replaybook shadow score"
        ),
        "shadow_dir": str(out),
        **stats,
    }


def shadow_score(out: Path, data: Path = DEFAULT_DATA_DIR, extract: str = "none") -> dict[str, Any]:
    """Score stored proposals whose tickets engineers have since resolved."""
    from replaybook.adapters.synthetic import load_platform, load_tickets
    from replaybook.eval.extract import make_extractor
    from replaybook.eval.shadow import score

    data = Path(data)
    out = Path(out)
    if not (data / "tickets.jsonl").is_file():
        raise WorkflowError(f"no tickets.jsonl in {data}")
    if not (out / "proposals.jsonl").is_file():
        raise WorkflowError(f"no proposals.jsonl in {out}; run `replaybook shadow record` first")
    try:
        extractor = make_extractor(extract)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    from replaybook.eval.runner import failure_line, result_line

    stats = score(load_tickets(data), load_platform(data), out, extractor=extractor)
    rep = stats["report"]
    s = rep.summary
    lines = [
        f"scored {stats['scored_now']} new proposal(s); {rep.scored} scored in total, "
        f"{rep.pending} pending, {rep.unscorable} unscorable. "
        f"{result_line(s, 'scored proposals')}; {failure_line(s)} (pass rate "
        f"{s.pass_rate:.0%}, 95% CI {s.wilson_low:.0%}-{s.wilson_high:.0%}); unsafe "
        f"proposals caught: {len(rep.unsafe_caught)}"
    ]
    for u in rep.unsafe_caught:
        lines.append(
            f"  UNSAFE {u['ticket_id']}: {', '.join(u['tools'])} ({u['severity']}) {u['detail']}"
        )
    return {
        "message": "\n".join(lines),
        "shadow_dir": str(out),
        "scored_now": stats["scored_now"],
        "scored": rep.scored,
        "pending": rep.pending,
        "unscorable": rep.unscorable,
        "summary": s.model_dump(),
        "unsafe_caught": list(rep.unsafe_caught),
    }


def shadow_report(out: Path) -> dict[str, Any]:
    """The shadow summary (what the evidence pack shows under Shadow mode)."""
    from replaybook.eval.runner import result_line
    from replaybook.eval.shadow import load_report

    out = Path(out)
    rep = load_report(out)
    if rep is None:
        raise WorkflowError(f"no shadow.json in {out}")
    s = rep.summary
    lines = [
        f"{', '.join(rep.request_types) or '-'}: {rep.proposals} proposals, {rep.scored} "
        f"scored, {rep.pending} pending, {rep.unscorable} unscorable; "
        f"{result_line(s, 'scored proposals')}; pass rate "
        f"{s.pass_rate:.0%} (95% CI {s.wilson_low:.0%}-{s.wilson_high:.0%}), critical "
        f"{s.critical_failures}, unsafe caught {len(rep.unsafe_caught)}"
    ]
    for g in rep.groups:
        lines.append(f"  {g.id} ({g.count}) {g.label}")
    return {
        "message": "\n".join(lines),
        "shadow_dir": str(out),
        "request_types": list(rep.request_types),
        "proposals": rep.proposals,
        "scored": rep.scored,
        "pending": rep.pending,
        "unscorable": rep.unscorable,
        "summary": s.model_dump(),
        "unsafe_caught": list(rep.unsafe_caught),
        "groups": [{"id": g.id, "count": g.count, "label": g.label} for g in rep.groups],
        "agents": list(rep.agents),
    }


def packs() -> dict[str, Any]:
    """The loaded packs and their request types (what `build_testset` accepts)."""
    from replaybook.packs import registry

    reg = registry()
    return {
        "message": "packs: "
        + ", ".join(f"{p.name} ({len(p.request_types)} types)" for p in reg.packs),
        "search_path": [str(p) for p in reg.search_path],
        "packs": [
            {
                "name": p.name,
                "path": str(p.path),
                "description": p.description,
                "request_types": [
                    {"key": k, "label": v.label, "skill": v.skill is not None}
                    for k, v in p.request_types.items()
                ],
            }
            for p in reg.packs
        ],
    }


def labels_sample(
    testset: Path, out: Path | None = None, labels_file: Path = DEFAULT_LABELS
) -> dict[str, Any]:
    """The cases a person should spot-check on a test set (at least 10 or 10%, whichever is
    larger), with a Markdown review sheet. Cases an earlier check in `labels_file` covered
    come last, so the sample is a fresh one where the set allows. Read-only: recording the
    check is `replaybook labels check`, by a person."""
    from replaybook.eval.golive import load_label_checks
    from replaybook.eval.labels import required_sample, review_sheet, suggest_sample

    test_set = load_test_set(testset)
    store = load_label_checks(Path(labels_file))
    ids = suggest_sample(test_set, store)
    sheet = review_sheet(test_set, ids)
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(sheet, encoding="utf-8")
    by_id = {c.id: c for c in test_set.cases}
    earlier = store.for_set(test_set.name)
    return {
        "message": (
            f"{test_set.name}: check these {len(ids)} of {len(test_set.cases)} cases "
            f"(at least {required_sample(len(test_set.cases))}): {', '.join(ids)}"
            + (f" -> {out}" if out is not None else "")
            + (f" (cases the {len(earlier)} earlier check(s) covered come last)" if earlier else "")
        ),
        "test_set": test_set.name,
        "cases": [
            {
                "case_id": i,
                "label": by_id[i].expectation.outcome,
                "reasons": list(by_id[i].expectation.outcome_reasons),
                "rule_parts": list(by_id[i].expectation.outcome_parts),
                "summary": by_id[i].summary,
            }
            for i in ids
        ],
        "sheet": sheet if out is None else str(out),
    }


def skills_validate(request_type: str = "", file: Path | None = None) -> dict[str, Any]:
    """Validate skills: every write a skill lists has a read-only check whose templates name
    the write's own arguments. All skills of the loaded packs, one request type, or a
    SKILL.md file. `valid` is False when any skill has a problem."""
    from replaybook.checks import parse_checks, validate_skill
    from replaybook.mock_env.registry import describe_all_tools
    from replaybook.packs import registry
    from replaybook.skills import SkillError, parse_skill, short, skill_path

    described = describe_all_tools()
    targets: list[tuple[str, Path]] = []
    if file is not None:
        if not Path(file).is_file():
            raise WorkflowError(f"no skill file at {file}")
        targets.append(("", Path(file)))
    elif request_type:
        try:
            targets.append((request_type, skill_path(request_type)))
        except KeyError as exc:
            raise WorkflowError(str(exc)) from exc
    else:
        targets.extend(sorted(registry().skills().items()))
    skills = []
    for key, path in targets:
        text = Path(path).read_text(encoding="utf-8")
        try:
            skill = parse_skill(text, Path(path), key)
            problems = validate_skill(skill, described)
            checks = parse_checks(skill)[0]
        except SkillError as exc:
            skill, problems, checks = None, [str(exc)], {}
        skills.append(
            {
                "request_type": skill.request_type if skill else key,
                "skill": skill.name if skill else None,
                "version": skill.version if skill else None,
                "path": str(path),
                "valid": not problems,
                "problems": problems,
                "checks": [c.as_dict() for c in checks.values()],
            }
        )
    lines = []
    for item in skills:
        name = f"{item['request_type'] or '-'} ({item['skill']} {short(item['version'])})"
        if item["valid"]:
            lines.append(f"{name}: valid, {len(item['checks'])} write(s) with a check")
        else:
            lines.append(f"{name}: INVALID")
            lines.extend(f"  - {problem}" for problem in item["problems"])
    if not skills:
        lines.append("no skills found")
    return {
        "message": "\n".join(lines),
        "valid": all(item["valid"] for item in skills),
        "skills": skills,
    }


# --- live runs after go-live -----------------------------------------------------------


def live_record(
    run: Any, live_dir: Path | None = None, discovery: Path | None = None
) -> dict[str, Any]:
    """Record one live run a team's runtime reports (a dict in the LiveRunReport shape).
    CLI only: `replaybook mcp` gets the report, not the recording."""
    from pydantic import ValidationError

    from replaybook.eval.live import LiveRunReport, record_run
    from replaybook.eval.live import live_dir as resolve

    directory = resolve(live_dir)
    try:
        report = LiveRunReport.model_validate(run)
        recorded = record_run(report, directory, discovery)
    except (ValidationError, ValueError) as exc:
        raise WorkflowError(f"the live run was not recorded: {exc}") from exc
    hours = "unknown" if recorded.hours_saved is None else f"{recorded.hours_saved:.2f}"
    return {
        "message": (
            f"{recorded.id}: {recorded.skill} on {recorded.ticket}, "
            f"{recorded.approval.decision} by {recorded.approval.by}, verification "
            f"{recorded.verification}"
            f"{', handed back' if recorded.escalated else ''}; hours saved {hours} "
            f"({recorded.hours_basis}) -> {directory}"
        ),
        "run": recorded.model_dump(mode="json"),
        "live_dir": str(directory),
    }


def live_report(
    live_dir: Path | None = None,
    window: int | None = None,
    skill: str = "",
    min_approval_rate: float | None = None,
    min_verification_rate: float | None = None,
) -> dict[str, Any]:
    """Each skill's rolling approval, verification and escalation rates over the window, the
    hours saved and whether it is degraded. Read-only."""
    from replaybook.eval.live import build_report
    from replaybook.eval.live import live_dir as resolve

    directory = resolve(live_dir)
    try:
        report = build_report(
            directory,
            window,
            min_approval_rate,
            min_verification_rate,
            skills=[skill] if skill else None,
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    lines = [s.line() for s in report.skills] or [f"no live runs recorded in {directory}"]
    if report.degraded:
        lines.append(
            f"{len(report.degraded)} skill(s) DEGRADED (approval below "
            f"{report.min_approval_rate:.0%} or verification below "
            f"{report.min_verification_rate:.0%}); moving a skill back to dry-run is a manual "
            "step (docs/go-live.md)"
        )
    return {
        "message": "\n".join(lines),
        "live_dir": str(directory),
        "report": report.model_dump(mode="json"),
        "degraded": [s.skill for s in report.degraded],
    }


def live_check(
    live_dir: Path | None = None,
    window: int | None = None,
    min_approval_rate: float | None = None,
    min_verification_rate: float | None = None,
) -> dict[str, Any]:
    """The live report for CI or a scheduler: exit_code 1 when a skill is degraded. Changes of
    state are appended to degraded.jsonl."""
    from replaybook.eval.live import build_report, note_transitions
    from replaybook.eval.live import live_dir as resolve

    result = live_report(live_dir, window, "", min_approval_rate, min_verification_rate)
    directory = resolve(live_dir)
    report = build_report(directory, window, min_approval_rate, min_verification_rate)
    written = note_transitions(directory, report)
    result["transitions"] = written
    result["exit_code"] = 1 if report.degraded else 0
    return result


# --- approval requests --------------------------------------------------------------------


def approval_request(
    skill: str,
    plan: Any,
    out: Path,
    ticket: str | None = None,
    runs: Path = Path("runs"),
    evidence: Path = Path("evidence"),
    live_dir: Path | None = None,
    labels_file: Path = DEFAULT_LABELS,
    min_pass_rate: float | None = None,
    window: int | None = None,
) -> dict[str, Any]:
    """Write what an approver sees before approving a skill's planned writes (Markdown and
    JSON). exit_code 1 when the request is blocked: the skill's current version has no
    replay, its replay failed the go-live gate, or the plan holds a write the skill does not
    cover. Nothing is approved here; a person decides in the team's approval channel."""
    from replaybook.approval import ApprovalError, build_request, parse_plan, write_request
    from replaybook.eval.golive import load_label_checks
    from replaybook.eval.live import live_dir as resolve

    try:
        request = build_request(
            skill,
            parse_plan(plan),
            ticket=ticket,
            runs_dir=Path(runs),
            evidence_dir=Path(evidence),
            live_dir=resolve(live_dir),
            label_checks=load_label_checks(Path(labels_file)),
            min_pass_rate=min_pass_rate,
            window=window,
        )
    except (ApprovalError, ValueError) as exc:
        raise WorkflowError(str(exc)) from exc
    paths = write_request(request, Path(out))
    head = (
        "BLOCKED: " + "; ".join(request["blocks"])
        if request["blocked"]
        else "ready for a person's decision"
    )
    replay = request["replay"]
    replayed = (
        f"{replay['result_line']} on {replay['created_at'][:10]}"
        if replay
        else "none for this version"
    )
    lines = [
        f"approval request for {request['skill']} {request['skill_version_short']}"
        f"{' on ' + ticket if ticket else ''}: {head}",
        f"  replay: {replayed}",
        f"  live verification: {request['live_line']}",
        f"  evidence pack: {request['evidence_pack'] or 'none found for this replay'}",
        *[f"  {w}" for w in request["warnings"]],
        f"  -> {paths['markdown']} and {paths['json']}",
    ]
    return {
        "message": "\n".join(lines),
        "blocked": request["blocked"],
        "blocks": request["blocks"],
        "markdown": str(paths["markdown"]),
        "json": str(paths["json"]),
        "request": request,
        "exit_code": 1 if request["blocked"] else 0,
    }
