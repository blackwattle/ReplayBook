"""Replay a test set against an agent: parallel, resumable, deterministic in its output.

Each case gets a fresh copy of its world and its own recorder. Results are appended to
`cases.jsonl` as they finish so an interrupted run can resume, and the final `results.json`
is sorted by case id so thread scheduling never changes the output.

A replay is always a dry run. Before it starts, the request type's skill must pass
validation (every write it lists has a read-only check, see replaybook.checks); each case
result then lists the agent's planned writes with the check that would confirm each one in
a live run. No check is ever run here.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from replaybook import netguard
from replaybook.agents.base import AgentRunResult, AgentUnderTest
from replaybook.checks import Check, checks_for, planned_checks, require_valid_skill
from replaybook.eval.accepted import AcceptedDifferences, apply_accepted
from replaybook.eval.scoring import FAILING, OUTCOME_LABELS, OUTCOMES, CaseResult, score_case
from replaybook.eval.stats import wilson_interval
from replaybook.mock_env.guarded import describe_packs
from replaybook.mock_env.knowledge import Corpus
from replaybook.mock_env.registry import (
    MockEnvironment,
    describe_all_tools,
    tool_schema_fingerprint,
)
from replaybook.packs import registry
from replaybook.schema.testcase import TestCase, TestSet
from replaybook.skills import skill_identity

CASES_FILE = "cases.jsonl"
RESULTS_FILE = "results.json"


class RunSummary(BaseModel):
    total: int = 0
    # match + correct_escalation; accepted differences are counted on their own.
    passed: int = 0
    # partial, mismatch, unsafe, missed and unneeded escalations.
    failed: int = 0
    accepted: int = 0
    errors: int = 0
    # (passed + accepted) / total, with its 95% Wilson interval.
    pass_rate: float = 0.0
    wilson_low: float = 0.0
    wilson_high: float = 0.0
    critical_failures: int = 0
    # No critical unsafe action and no agent error. `check` and `replay` base their exit
    # codes on it, as before spec v5; the go-live gate (eval.golive) is the stricter one.
    gate: str = "pass"
    by_reason: dict[str, int] = Field(default_factory=dict)
    # Spec v5: the count of every outcome (scoring.OUTCOMES), how many cases expected an
    # escalation or an act, and the unneeded-escalation rate: unneeded escalations over the
    # cases that expected the agent to act, a usefulness measure beside the pass rate.
    # Empty in summaries written before v5.
    outcomes: dict[str, int] = Field(default_factory=dict)
    escalate_cases: int = 0
    act_cases: int = 0
    unneeded_escalation_rate: float = 0.0


class RunResult(BaseModel):
    run_id: str
    created_at: datetime
    agent_config: dict[str, Any]
    test_set_name: str
    test_set_request_type: str
    test_set_sha256: str
    tool_fingerprint: str
    fingerprint: str
    seed: int = 0
    workers: int = 1
    # Air-gapped mode state of the process that made the run (see replaybook.netguard).
    environment: dict[str, Any] = Field(default_factory=dict)
    # Model calls across the run, when the agent used a model: calls, cached_calls, cost_usd
    # (known costs only), unknown_cost_calls, cost_sources, models, cost_known. None for
    # agents without a model.
    model_usage: dict[str, Any] | None = None
    # The skill of the test set's request type at the time of the run: its name and
    # skill_version (the SHA-256 of its SKILL.md). A replay counts only for the version it
    # ran. None in runs written before spec v5 and for request types without a skill.
    skill: str | None = None
    skill_version: str | None = None
    results: list[CaseResult] = Field(default_factory=list)
    summary: RunSummary = Field(default_factory=RunSummary)

    def result(self, case_id: str) -> CaseResult:
        for r in self.results:
            if r.case_id == case_id:
                return r
        raise KeyError(case_id)


def compute_fingerprint(
    agent_config: dict[str, Any],
    test_set_sha: str,
    tool_fingerprint: str,
    skill_version: str | None = None,
) -> str:
    """Hashes model id, skill text, tool schemas, agent config and test set (via their
    hashes). The skill's version is in it for every agent, scripted ones included, so an
    edited skill always needs a new replay."""
    data: dict[str, Any] = {
        "agent": agent_config,
        "test_set": test_set_sha,
        "tools": tool_fingerprint,
    }
    if skill_version:
        data["skill"] = skill_version
    payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summarise(results: Iterable[CaseResult]) -> RunSummary:
    items = list(results)
    summary = RunSummary(total=len(items))
    counts = dict.fromkeys(OUTCOMES, 0)
    for r in items:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
        if r.outcome in ("match", "correct_escalation"):
            summary.passed += 1
        elif r.outcome == "accepted":
            summary.accepted += 1
        elif r.outcome == "error":
            summary.errors += 1
        else:
            summary.failed += 1
        if (r.outcome in FAILING or r.outcome == "error") and r.reason:
            summary.by_reason[r.reason] = summary.by_reason.get(r.reason, 0) + 1
        if r.is_critical:
            summary.critical_failures += 1
        if r.expected_outcome == "escalate":
            summary.escalate_cases += 1
        else:
            summary.act_cases += 1
    ok = summary.passed + summary.accepted
    summary.pass_rate = ok / summary.total if summary.total else 0.0
    summary.wilson_low, summary.wilson_high = wilson_interval(ok, summary.total)
    summary.gate = "fail" if summary.critical_failures or summary.errors else "pass"
    summary.by_reason = dict(sorted(summary.by_reason.items()))
    summary.outcomes = counts
    summary.unneeded_escalation_rate = (
        counts["unneeded_escalation"] / summary.act_cases if summary.act_cases else 0.0
    )
    return summary


def result_line(summary: RunSummary, noun: str = "") -> str:
    """'passed X of N (A matched, B correctly escalated, C accepted)': how reports, check,
    the evidence pack, approval requests and `replaybook mcp` state a result. Accepted
    differences are named only when there are some; `noun` follows N ('past tickets')."""
    o = summary.outcomes
    matched = o.get("match", 0) if o else summary.passed
    parts = [f"{matched} matched", f"{o.get('correct_escalation', 0)} correctly escalated"]
    if summary.accepted:
        parts.append(f"{summary.accepted} accepted")
    ok = summary.passed + summary.accepted
    of = f"{summary.total} {noun}".strip()
    return f"passed {ok} of {of} ({', '.join(parts)})"


def failure_line(summary: RunSummary) -> str:
    """The cases that did not pass, by outcome: 'failed 3 (2 mismatch, 1 unsafe)'."""
    o = summary.outcomes
    named = [f"{o[k]} {OUTCOME_LABELS[k]}" for k in OUTCOMES if k in FAILING and o.get(k)]
    text = f"failed {summary.failed}" + (f" ({', '.join(named)})" if named else "")
    if summary.errors:
        text += f"; {summary.errors} agent error{'s' if summary.errors != 1 else ''}"
    return text


def usefulness_line(summary: RunSummary) -> str:
    """The unneeded-escalation rate, the usefulness measure beside the pass rate."""
    unneeded = summary.outcomes.get("unneeded_escalation", 0)
    return (
        f"unneeded escalations {summary.unneeded_escalation_rate:.0%} "
        f"({unneeded} of {summary.act_cases} cases that expected the agent to act)"
    )


def replay_case(
    case: TestCase,
    agent: AgentUnderTest,
    knowledge: Corpus | None = None,
    checks: dict[str, Check] | None = None,
) -> CaseResult:
    env = MockEnvironment(case.world.model_copy(deep=True), knowledge=knowledge)
    started = time.perf_counter()
    error: str | None = None
    usage: dict[str, Any] | None = None
    try:
        outcome: AgentRunResult = agent.run(env)
        stopped = outcome.stopped_reason
        transcript = outcome.transcript
        usage = outcome.usage
        if outcome.stopped_reason in ("error", "offline") and outcome.error:
            error = outcome.error
    except Exception as exc:  # the agent is someone else's code; never let it kill the run
        stopped = "error"
        transcript = []
        error = f"{type(exc).__name__}: {exc}"
    result = score_case(
        case,
        env.recorder.writes,
        reads=len(env.recorder.reads),
        agent_stopped_reason=stopped,
        agent_error=error,
    )
    result.transcript = transcript
    result.model_usage = usage
    result.planned_checks = planned_checks(checks or {}, env.recorder.writes)
    result.duration_s = round(time.perf_counter() - started, 4)
    return result


def model_usage(results: Iterable[CaseResult]) -> dict[str, Any] | None:
    """The run's model calls added up, or None when no case used a model."""
    used = [r.model_usage for r in results if r.model_usage]
    if not used:
        return None
    unknown = sum(int(u.get("unknown_cost_calls", 0)) for u in used)
    sources = sorted({s for u in used for s in u.get("cost_sources", [])})
    return {
        "calls": sum(int(u.get("calls", 0)) for u in used),
        "cached_calls": sum(int(u.get("cached_calls", 0)) for u in used),
        "cost_usd": round(sum(float(u.get("cost_usd") or 0.0) for u in used), 6),
        "unknown_cost_calls": unknown,
        "cost_sources": sources,
        "models": sorted({m for u in used for m in u.get("models", [])}),
        "cost_known": unknown == 0,
    }


def run_replay(
    test_set: TestSet,
    agent: AgentUnderTest,
    out_dir: Path,
    workers: int = 4,
    resume: bool = True,
    seed: int = 0,
    run_id: str | None = None,
    accepted: AcceptedDifferences | None = None,
    today: date | None = None,
    knowledge: Corpus | None = None,
) -> RunResult:
    """`knowledge` is the past-ticket corpus the agent may search, served behind the
    time-travel guard (only items created before each case's ticket)."""
    described = describe_all_tools()
    require_valid_skill(test_set.request_type, described)
    checks = checks_for(test_set.request_type)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_path = out_dir / CASES_FILE
    if hasattr(agent, "set_seed"):
        agent.set_seed(seed)

    done: dict[str, CaseResult] = {}
    if resume and cases_path.exists():
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = CaseResult.model_validate_json(line)
                done[r.case_id] = r
    elif cases_path.exists():
        cases_path.unlink()

    todo = [c for c in test_set.cases if c.id not in done]
    lock = threading.Lock()

    def work(case: TestCase) -> CaseResult:
        result = replay_case(case, agent, knowledge, checks)
        with lock, cases_path.open("a", encoding="utf-8") as handle:
            handle.write(result.model_dump_json())
            handle.write("\n")
        return result

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(work, case) for case in todo]
            for future in as_completed(futures):
                result = future.result()
                done[result.case_id] = result

    results = [done[c.id] for c in sorted(test_set.cases, key=lambda c: c.id)]
    if accepted is not None:
        results = apply_accepted(results, accepted, today)
    tool_fingerprint = tool_schema_fingerprint(described)
    test_set_sha = test_set.sha256()
    environment = netguard.summary()
    environment["knowledge"] = str(knowledge.path) if knowledge and knowledge.path else None
    # Knowledge packs read a team's own (live) search server: say which, and whether its
    # answers were pinned (cache: replay) or could have changed since.
    environment["knowledge_packs"] = describe_packs(registry().packs)
    skill = skill_identity(test_set.request_type)
    run = RunResult(
        run_id=run_id or out_dir.name,
        created_at=datetime.now(UTC),
        agent_config=dict(agent.config),
        test_set_name=test_set.name,
        test_set_request_type=test_set.request_type,
        test_set_sha256=test_set_sha,
        tool_fingerprint=tool_fingerprint,
        fingerprint=compute_fingerprint(
            dict(agent.config), test_set_sha, tool_fingerprint, skill["skill_version"]
        ),
        seed=seed,
        workers=workers,
        environment=environment,
        model_usage=model_usage(results),
        skill=skill["skill"],
        skill_version=skill["skill_version"],
        results=results,
        summary=summarise(results),
    )
    (out_dir / RESULTS_FILE).write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return run


def load_run(out_dir: Path) -> RunResult:
    run = RunResult.model_validate_json((Path(out_dir) / RESULTS_FILE).read_text(encoding="utf-8"))
    if not run.summary.outcomes:
        # Written before spec v5: its results were read into the outcome classes, so count
        # them again for the summary.
        run.summary = summarise(run.results)
    return run


def reapply_accepted(
    out_dir: Path, accepted: AcceptedDifferences, today: date | None = None
) -> RunResult:
    """Re-read a finished run, apply the current acceptances and rewrite results.json."""
    run = load_run(out_dir)
    run.results = apply_accepted(run.results, accepted, today)
    run.summary = summarise(run.results)
    (Path(out_dir) / RESULTS_FILE).write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return run
