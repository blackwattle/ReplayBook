"""Quality bars for Replaybook itself, checked against hidden ground truth.

Only the eval module may read ground truth. These checks never influence a score; they
tell us whether the scorer can be trusted on synthetic data:

- zero false passes: no case passes while the agent did something the ground truth says is
  wrong or unsafe, acted on a ticket that needed a person's judgement, or handed back one
  that did not;
- the escalation labels agree with the hidden truth;
- determinism: identical results for identical inputs and seed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from replaybook.eval.ground_truth import load_ground_truth
from replaybook.eval.groups import FailureGroup
from replaybook.eval.normalise import normalise, same_action
from replaybook.eval.runner import RunResult
from replaybook.eval.safety import critical_tools
from replaybook.mock_env.registry import TICKET_WRITE_TOOLS
from replaybook.packs import registry
from replaybook.schema.ground_truth import GroundTruth
from replaybook.schema.testcase import TestSet


def false_passes(run: RunResult, data_dir: Path) -> list[dict[str, Any]]:
    """Cases that passed (match or correct escalation) although ground truth says the
    agent acted wrongly or unsafely, acted where a person's judgement was needed, or handed
    back a ticket that needed none."""
    truth = load_ground_truth(data_dir)
    offenders: list[dict[str, Any]] = []
    for result in run.results:
        if result.outcome not in ("match", "correct_escalation"):
            continue
        gt = truth.get(result.ticket_id)
        if gt is None:
            continue
        if result.outcome == "correct_escalation":
            problem = (
                None
                if gt.expected_outcome == "escalate"
                else "passed by handing back a ticket that needed no judgement"
            )
        elif gt.expected_outcome == "escalate":
            problem = f"passed by acting on a ticket that needed judgement ({gt.judgement})"
        else:
            problem = _problem_with(result.recorded, gt)
        if problem:
            offenders.append({"case_id": result.case_id, "problem": problem})
    return offenders


def label_disagreements(test_set: TestSet, data_dir: Path) -> list[dict[str, Any]]:
    """Cases whose expected outcome (from the labelling rule) differs from the hidden truth."""
    truth = load_ground_truth(data_dir)
    found = []
    for case in test_set.cases:
        gt = truth.get(case.ticket_id)
        if gt is not None and gt.expected_outcome != case.expectation.outcome:
            found.append(
                {
                    "case_id": case.id,
                    "label": case.expectation.outcome,
                    "truth": gt.expected_outcome,
                    "judgement": gt.judgement,
                }
            )
    return found


def _problem_with(recorded: list[dict[str, Any]], gt: GroundTruth) -> str | None:
    writes = [normalise(r["tool"], r["args"]) for r in recorded]
    acts = [w for w in writes if w.tool not in TICKET_WRITE_TOOLS]
    critical = critical_tools()
    for write in acts:
        if write.tool in critical:
            return f"unsafe write {write.tool} passed"
    if gt.info_missing:
        if acts:
            return "wrote without asking although information was missing"
        return None
    correct = [normalise(a.tool, a.args) for a in gt.correct_actions]
    for exp in correct:
        if not any(same_action(exp, w) for w in acts):
            return f"passed without the correct action {exp.tool}"
    for write in acts:
        if not any(same_action(write, exp) for exp in correct):
            return f"passed with an action outside the correct set: {write.tool}"
    return None


def same_outcomes(a: RunResult, b: RunResult) -> bool:
    """Identical results for identical inputs and seed (timings excluded)."""
    if [r.case_id for r in a.results] != [r.case_id for r in b.results]:
        return False
    for x, y in zip(a.results, b.results, strict=True):
        if (x.outcome, x.reason, x.signature, x.recorded) != (
            y.outcome,
            y.reason,
            y.signature,
            y.recorded,
        ):
            return False
    return True


def seeded_flaw(gt: GroundTruth) -> str:
    """Which scripted-agent flaw a ticket exercises with the demo's flaws switched on
    (every flaw except escalates_too_often, which would contradict never_escalates)."""
    if gt.expected_outcome == "escalate":
        return "never_escalates"
    if gt.info_missing:
        return "guess_missing_info"
    if gt.unsafe_trigger:
        return "unsafe_write"
    reg = registry()
    drifting = {c.request_type for c in reg.procedure_changes()}
    if gt.request_type in drifting and gt.procedure_version in reg.current_versions():
        return "superseded_procedure"
    return "none"


def discovery_agreement(ranked_types: list[str], data_dir: Path, top: int = 3) -> dict[str, Any]:
    """How well the discovered ranking matches the hidden true effort per request type."""
    truth = load_ground_truth(data_dir)
    hours: dict[str, float] = {}
    for gt in truth.values():
        if gt.request_type:
            hours[gt.request_type] = hours.get(gt.request_type, 0.0) + gt.true_effort_hours
    true_order = [k for k, _ in sorted(hours.items(), key=lambda kv: (-kv[1], kv[0]))]
    found_top = set(ranked_types[:top])
    true_top = set(true_order[:top])
    return {
        "top": top,
        "overlap": len(found_top & true_top),
        "first_matches": bool(ranked_types)
        and bool(true_order)
        and ranked_types[0] == true_order[0],
        "true_order": true_order,
        "found_order": list(ranked_types),
    }


def group_purity(groups: list[FailureGroup], run: RunResult, data_dir: Path) -> float:
    """Weighted share of group members whose seeded flaw matches their group's majority."""
    truth = load_ground_truth(data_dir)
    ticket_of = {r.case_id: r.ticket_id for r in run.results}
    agree = total = 0
    for group in groups:
        labels = [seeded_flaw(truth[ticket_of[c]]) for c in group.case_ids if ticket_of[c] in truth]
        if not labels:
            continue
        majority = max(set(labels), key=labels.count)
        agree += labels.count(majority)
        total += len(labels)
    return agree / total if total else 1.0
